"""HTTP/1.1 client library

<intro stuff goes here>
<other stuff, too>

HTTPConnection go through a number of "states", which defines when a client
may legally make another request or fetch the response for a particular
request. This diagram details these state transitions:

    (null)
      |
      | HTTPConnection()
      v
    Idle
      |
      | putrequest()
      v
    Request-started
      |
      | ( putheader() )*  endheaders()
      v
    Request-sent
      |
      | response = getresponse()
      v
    Unread-response   [Response-headers-read]
      |\____________________
      |                     |
      | response.read()     | putrequest()
      v                     v
    Idle                  Req-started-unread-response
                     ______/|
                   /        |
   response.read() |        | ( putheader() )*  endheaders()
                   v        v
       Request-started    Req-sent-unread-response
                            |
                            | response.read()
                            v
                          Request-sent

This diagram presents the following rules:
  -- a second request may not be started until {response-headers-read}
  -- a response [object] cannot be retrieved until {request-sent}
  -- there is no differentiation between an unread response body and a
     partially read response body

Note: this enforcement is applied by the HTTPConnection class. The
      HTTPResponse class does not enforce this state machine, which
      implies sophisticated clients may accelerate the request/response
      pipeline. Caution should be taken, though: accelerating the states
      beyond the above pattern may imply knowledge of the server's
      connection-close behavior for certain requests. For example, it
      is impossible to tell whether the server will close the connection
      UNTIL the response headers have been read; this means that further
      requests cannot be placed into the pipeline until it is known that
      the server will NOT be closing the connection.

Logical State                  __state            __response
-------------                  -------            ----------
Idle                           _CS_IDLE           None
Request-started                _CS_REQ_STARTED    None
Request-sent                   _CS_REQ_SENT       None
Unread-response                _CS_IDLE           <response_class>
Req-started-unread-response    _CS_REQ_STARTED    <response_class>
Req-sent-unread-response       _CS_REQ_SENT       <response_class>
"""

import errno
import mimetools
import socket
import string

try:
    from io import StringIO
except ImportError:
    from io import StringIO

__all__ = ["HTTP", "HTTPResponse", "HTTPConnection", "HTTPSConnection",
           "HTTPException", "NotConnected", "UnknownProtocol",
           "UnknownTransferEncoding", "IllegalKeywordArgument",
           "UnimplementedFileMode", "IncompleteRead", "InvalidURL",
           "ImproperConnectionState", "CannotSendRequest", "CannotSendHeader",
           "ResponseNotReady", "BadStatusLine", "error"]

HTTP_PORT = 80
HTTPS_PORT = 443

_UNKNOWN = 'UNKNOWN'

# connection states
_CS_IDLE = 'Idle'
_CS_REQ_STARTED = 'Request-started'
_CS_REQ_SENT = 'Request-sent'


class HTTPResponse:
    def __init__(self, sock, debuglevel=0):
        self.fp = sock.makefile('rb', 0)
        self.debuglevel = debuglevel

        self.msg = None

        # from the Status-Line of the response
        self.version = _UNKNOWN # HTTP-Version
        self.status = _UNKNOWN  # Status-Code
        self.reason = _UNKNOWN  # Reason-Phrase

        self.chunked = _UNKNOWN         # is "chunked" being used?
        self.chunk_left = _UNKNOWN      # bytes left to read in current chunk
        self.length = _UNKNOWN          # number of bytes left in response
        self.will_close = _UNKNOWN      # conn will close at end of response

    def begin(self):
        if self.msg is not None:
            # we've already started reading the response
            return

        line = self.fp.readline()
        if self.debuglevel > 0:
            print("reply:", repr(line))
        try:
            [version, status, reason] = string.split(line, None, 2)
        except ValueError:
            try:
                [version, status] = string.split(line, None, 1)
                reason = ""
            except ValueError:
                version = "HTTP/0.9"
                status = "200"
                reason = ""
        if version[:5] != 'HTTP/':
            self.close()
            raise BadStatusLine(line)

        # The status code is a three-digit number
        try:
            self.status = status = int(status)
            if status < 100 or status > 999:
                raise BadStatusLine(line)
        except ValueError:
            raise BadStatusLine(line)
        self.reason = string.strip(reason)

        if version == 'HTTP/1.0':
            self.version = 10
        elif startswith(version, 'HTTP/1.'):
            self.version = 11   # use HTTP/1.1 code for HTTP/1.x where x>=1
        elif version == 'HTTP/0.9':
            self.version = 9
        else:
            raise UnknownProtocol(version)

        if self.version == 9:
            self.msg = mimetools.Message(StringIO())
            return

        self.msg = mimetools.Message(self.fp, 0)
        if self.debuglevel > 0:
            for hdr in self.msg.headers:
                print("header:", hdr, end=' ')

        # don't let the msg keep an fp
        self.msg.fp = None

        # are we using the chunked-style of transfer encoding?
        tr_enc = self.msg.getheader('transfer-encoding')
        if tr_enc:
            if string.lower(tr_enc) != 'chunked':
                raise UnknownTransferEncoding()
            self.chunked = 1
            self.chunk_left = None
        else:
            self.chunked = 0

        # will the connection close at the end of the response?
        conn = self.msg.getheader('connection')
        if conn:
            conn = string.lower(conn)
            # a "Connection: close" will always close the connection. if we
            # don't see that and this is not HTTP/1.1, then the connection will
            # close unless we see a Keep-Alive header.
            self.will_close = string.find(conn, 'close') != -1 or \
                              ( self.version != 11 and \
                                not self.msg.getheader('keep-alive') )
        else:
            # for HTTP/1.1, the connection will always remain open
            # otherwise, it will remain open IFF we see a Keep-Alive header
            self.will_close = self.version != 11 and \
                              not self.msg.getheader('keep-alive')

        # do we have a Content-Length?
        # NOTE: RFC 2616, S4.4, #3 says we ignore this if tr_enc is "chunked"
        length = self.msg.getheader('content-length')
        if length and not self.chunked:
            try:
                self.length = int(length)
            except ValueError:
                self.length = None
        else:
            self.length = None

        # does the body have a fixed length? (of zero)
        if (status == 204 or            # No Content
            status == 304 or            # Not Modified
            100 <= status < 200):       # 1xx codes
            self.length = 0

        # if the connection remains open, and we aren't using chunked, and
        # a content-length was not provided, then assume that the connection
        # WILL close.
        if not self.will_close and \
           not self.chunked and \
           self.length is None:
            self.will_close = 1

    def close(self):
        if self.fp:
            self.fp.close()
            self.fp = None

    def isclosed(self):
        # NOTE: it is possible that we will not ever call self.close(). This
        #       case occurs when will_close is TRUE, length is None, and we
        #       read up to the last byte, but NOT past it.
        #
        # IMPLIES: if will_close is FALSE, then self.close() will ALWAYS be
        #          called, meaning self.isclosed() is meaningful.
        return self.fp is None

    def read(self, amt=None):
        if self.fp is None:
            return ''

        if self.chunked:
            return self._read_chunked(amt)

        if amt is None:
            # unbounded read
            if self.will_close:
                s = self.fp.read()
            else:
                s = self._safe_read(self.length)
            self.close()        # we read everything
            return s

        if self.length is not None:
            if amt > self.length:
                # clip the read to the "end of response"
                amt = self.length
            self.length = self.length - amt

        # we do not use _safe_read() here because this may be a .will_close
        # connection, and the user is reading more bytes than will be provided
        # (for example, reading in 1k chunks)
        s = self.fp.read(amt)

        return s

    def _read_chunked(self, amt):
        assert self.chunked != _UNKNOWN
        chunk_left = self.chunk_left
        value = ''

        # XXX This accumulates chunks by repeated string concatenation,
        # which is not efficient as the number or size of chunks gets big.
        while 1:
            if chunk_left is None:
                line = self.fp.readline()
                i = string.find(line, ';')
                if i >= 0:
                    line = line[:i] # strip chunk-extensions
                chunk_left = string.atoi(line, 16)
                if chunk_left == 0:
                    break
            if amt is None:
                value = value + self._safe_read(chunk_left)
            elif amt < chunk_left:
                value = value + self._safe_read(amt)
                self.chunk_left = chunk_left - amt
                return value
            elif amt == chunk_left:
                value = value + self._safe_read(amt)
                self._safe_read(2)  # toss the CRLF at the end of the chunk
                self.chunk_left = None
                return value
            else:
                value = value + self._safe_read(chunk_left)
                amt = amt - chunk_left

            # we read the whole chunk, get another
            self._safe_read(2)      # toss the CRLF at the end of the chunk
            chunk_left = None

        # read and discard trailer up to the CRLF terminator
        ### note: we shouldn't have any trailers!
        while 1:
            line = self.fp.readline()
            if line == '\r\n':
                break

        # we read everything; close the "file"
        # XXX Shouldn't the client close the file?
        self.close()

        return value

    def _safe_read(self, amt):
        """Read the number of bytes requested, compensating for partial reads.

        Normally, we have a blocking socket, but a read() can be interrupted
        by a signal (resulting in a partial read).

        Note that we cannot distinguish between EOF and an interrupt when zero
        bytes have been read. IncompleteRead() will be raised in this
        situation.

        This function should be used when <amt> bytes "should" be present for
        reading. If the bytes are truly not available (due to EOF), then the
        IncompleteRead exception can be used to detect the problem.
        """
        s = ''
        while amt > 0:
            chunk = self.fp.read(amt)
            if not chunk:
                raise IncompleteRead(s)
            s = s + chunk
            amt = amt - len(chunk)
        return s

    def getheader(self, name, default=None):
        if self.msg is None:
            raise ResponseNotReady()
        return self.msg.getheader(name, default)


class HTTPConnection:

    _http_vsn = 11
    _http_vsn_str = 'HTTP/1.1'

    response_class = HTTPResponse
    default_port = HTTP_PORT
    auto_open = 1
    debuglevel = 0

    def __init__(self, host, port=None):
        self.sock = None
        self.__response = None
        self.__state = _CS_IDLE

        self._set_hostport(host, port)

    def _set_hostport(self, host, port):
        if port is None:
            i = string.find(host, ':')
            if i >= 0:
                try:
                    port = int(host[i+1:])
                except ValueError:
                    raise InvalidURL("nonnumeric port: '%s'"%host[i+1:])
                host = host[:i]
            else:
                port = self.default_port
        self.host = host
        self.port = port

    def set_debuglevel(self, level):
        self.debuglevel = level

    def connect(self):
        """Connect to the host and port specified in __init__."""
        msg = "getaddrinfo returns an empty list"
        hostname, aliaslist, ipaddrlist = socket.gethostbyname_ex(self.host)
        for ipaddr in ipaddrlist:
            sa = (ipaddr, self.port)
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                if self.debuglevel > 0:
                    print("connect: (%s, %s)" % (self.host, self.port))
                self.sock.connect(sa)
            except socket.error as msg:
                if self.debuglevel > 0:
                    print('connect fail:', (self.host, self.port))
                if self.sock:
                    self.sock.close()
                self.sock = None
                continue
            break
        if not self.sock:
            raise socket.error(msg)

    def close(self):
        """Close the connection to the HTTP server."""
        if self.sock:
            self.sock.close()   # close it manually... there may be other refs
            self.sock = None
        if self.__response:
            self.__response.close()
            self.__response = None
        self.__state = _CS_IDLE

    def send(self, str):
        """Send `str' to the server."""
        if self.sock is None:
            if self.auto_open:
                self.connect()
            else:
                raise NotConnected()

        # send the data to the server. if we get a broken pipe, then close
        # the socket. we want to reconnect when somebody tries to send again.
        #
        # NOTE: we DO propagate the error, though, because we cannot simply
        #       ignore the error... the caller will know if they can retry.
        if self.debuglevel > 0:
            print("send:", repr(str))
        try:
            self.sock.send(str)
        except socket.error as v:
            if v[0] == 32:      # Broken pipe
                self.close()
            raise

    def putrequest(self, method, url, skip_host=0):
        """Send a request to the server.

        `method' specifies an HTTP request method, e.g. 'GET'.
        `url' specifies the object being requested, e.g. '/index.html'.
        """

        # check if a prior response has been completed
        if self.__response and self.__response.isclosed():
            self.__response = None

        #
        # in certain cases, we cannot issue another request on this connection.
        # this occurs when:
        #   1) we are in the process of sending a request.   (_CS_REQ_STARTED)
        #   2) a response to a previous request has signalled that it is going
        #      to close the connection upon completion.
        #   3) the headers for the previous response have not been read, thus
        #      we cannot determine whether point (2) is true.   (_CS_REQ_SENT)
        #
        # if there is no prior response, then we can request at will.
        #
        # if point (2) is true, then we will have passed the socket to the
        # response (effectively meaning, "there is no prior response"), and
        # will open a new one when a new request is made.
        #
        # Note: if a prior response exists, then we *can* start a new request.
        #       We are not allowed to begin fetching the response to this new
        #       request, however, until that prior response is complete.
        #
        if self.__state == _CS_IDLE:
            self.__state = _CS_REQ_STARTED
        else:
            raise CannotSendRequest()

        if not url:
            url = '/'
        str = '%s %s %s\r\n' % (method, url, self._http_vsn_str)

        try:
            self.send(str)
        except socket.error as v:
            # trap 'Broken pipe' if we're allowed to automatically reconnect
            if v[0] != 32 or not self.auto_open:
                raise
            # try one more time (the socket was closed; this will reopen)
            self.send(str)

        if self._http_vsn == 11:
            # Issue some standard headers for better HTTP/1.1 compliance

            if not skip_host:
                # this header is issued *only* for HTTP/1.1
                # connections. more specifically, this means it is
                # only issued when the client uses the new
                # HTTPConnection() class. backwards-compat clients
                # will be using HTTP/1.0 and those clients may be
                # issuing this header themselves. we should NOT issue
                # it twice; some web servers (such as Apache) barf
                # when they see two Host: headers

                # If we need a non-standard port,include it in the
                # header.  If the request is going through a proxy,
                # but the host of the actual URL, not the host of the
                # proxy.

                netloc = ''
                if startswith(url, 'http'):
                    nil, netloc, nil, nil, nil = urlsplit(url)

                if netloc:
                    self.putheader('Host', netloc)
                elif self.port == HTTP_PORT:
                    self.putheader('Host', self.host)
                else:
                    self.putheader('Host', "%s:%s" % (self.host, self.port))

            # note: we are assuming that clients will not attempt to set these
            #       headers since *this* library must deal with the
            #       consequences. this also means that when the supporting
            #       libraries are updated to recognize other forms, then this
            #       code should be changed (removed or updated).

            # we only want a Content-Encoding of "identity" since we don't
            # support encodings such as x-gzip or x-deflate.
            self.putheader('Accept-Encoding', 'identity')

            # we can accept "chunked" Transfer-Encodings, but no others
            # NOTE: no TE header implies *only* "chunked"
            #self.putheader('TE', 'chunked')

            # if TE is supplied in the header, then it must appear in a
            # Connection header.
            #self.putheader('Connection', 'TE')

        else:
            # For HTTP/1.0, the server will assume "not chunked"
            pass

    def putheader(self, header, value):
        """Send a request header line to the server.

        For example: h.putheader('Accept', 'text/html')
        """
        if self.__state != _CS_REQ_STARTED:
            raise CannotSendHeader()

        str = '%s: %s\r\n' % (header, value)
        self.send(str)

    def endheaders(self):
        """Indicate that the last header line has been sent to the server."""

        if self.__state == _CS_REQ_STARTED:
            self.__state = _CS_REQ_SENT
        else:
            raise CannotSendHeader()

        self.send('\r\n')

    def request(self, method, url, body=None, headers={}):
        """Send a complete request to the server."""

        try:
            self._send_request(method, url, body, headers)
        except socket.error as v:
            # trap 'Broken pipe' if we're allowed to automatically reconnect
            if v[0] != 32 or not self.auto_open:
                raise
            # try one more time
            self._send_request(method, url, body, headers)

    def _send_request(self, method, url, body, headers):
        # If headers already contains a host header, then define the
        # optional skip_host argument to putrequest().  The check is
        # harder because field names are case insensitive.
        if ('Host' in headers
            or [x for x in list(headers.keys()) if string.lower(x) == "host"]):
            self.putrequest(method, url, skip_host=1)
        else:
            self.putrequest(method, url)

        if body:
            self.putheader('Content-Length', str(len(body)))
        for hdr, value in list(headers.items()):
            self.putheader(hdr, value)
        self.endheaders()

        if body:
            self.send(body)

    def getresponse(self):
        "Get the response from the server."

        # check if a prior response has been completed
        if self.__response and self.__response.isclosed():
            self.__response = None

        #
        # if a prior response exists, then it must be completed (otherwise, we
        # cannot read this response's header to determine the connection-close
        # behavior)
        #
        # note: if a prior response existed, but was connection-close, then the
        # socket and response were made independent of this HTTPConnection
        # object since a new request requires that we open a whole new
        # connection
        #
        # this means the prior response had one of two states:
        #   1) will_close: this connection was reset and the prior socket and
        #                  response operate independently
        #   2) persistent: the response was retained and we await its
        #                  isclosed() status to become true.
        #
        if self.__state != _CS_REQ_SENT or self.__response:
            raise ResponseNotReady()

        if self.debuglevel > 0:
            response = self.response_class(self.sock, self.debuglevel)
        else:
            response = self.response_class(self.sock)

        response.begin()
        self.__state = _CS_IDLE

        if response.will_close:
            # this effectively passes the connection to the response
            self.close()
        else:
            # remember this, so we can tell when it is complete
            self.__response = response

        return response


class FakeSocket:
    def __init__(self, sock, ssl):
        self.__sock = sock
        self.__ssl = ssl

    def makefile(self, mode, bufsize=None):
        """Return a readable file-like object with data from socket.

        This method offers only partial support for the makefile
        interface of a real socket.  It only supports modes 'r' and
        'rb' and the bufsize argument is ignored.

        The returned object contains *all* of the file data
        """
        if mode != 'r' and mode != 'rb':
            raise UnimplementedFileMode()

        msgbuf = []
        while 1:
            try:
                buf = self.__ssl.read()
            except socket.sslerror as err:
                if err[0] == 'EOF':
                    break
                raise
            except socket.error as err:
                if err[0] == errno.EINTR:
                    continue
                raise
            if buf == '':
                break
            msgbuf.append(buf)
        return StringIO(string.join(msgbuf, ""))

    def send(self, stuff, flags = 0):
        return self.__ssl.write(stuff)

    def sendall(self, stuff, flags = 0):
        return self.__ssl.write(stuff)

    def recv(self, len = 1024, flags = 0):
        return self.__ssl.read(len)

    def __getattr__(self, attr):
        return getattr(self.__sock, attr)


class HTTPSConnection(HTTPConnection):
    "This class allows communication via SSL."

    default_port = HTTPS_PORT

    def __init__(self, host, port=None, **x509):
        keys = list(x509.keys())
        try:
            keys.remove('key_file')
        except ValueError:
            pass
        try:
            keys.remove('cert_file')
        except ValueError:
            pass
        if keys:
            raise IllegalKeywordArgument()
        HTTPConnection.__init__(self, host, port)
        self.key_file = x509.get('key_file')
        self.cert_file = x509.get('cert_file')

    def connect(self):
        "Connect to a host on a given (SSL) port."

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((self.host, self.port))
        realsock = sock
        if hasattr(sock, "_sock"):
            realsock = sock._sock
        # misa: XXX x509 client-side is broken in 2.2 anyway
        ssl = socket.ssl(realsock)
        self.sock = FakeSocket(sock, ssl)


class HTTP:
    "Compatibility class with httplib.py from 1.5."

    _http_vsn = 10
    _http_vsn_str = 'HTTP/1.0'

    debuglevel = 0

    _connection_class = HTTPConnection

    def __init__(self, host='', port=None):
        "Provide a default host, since the superclass requires one."

        # some joker passed 0 explicitly, meaning default port
        if port == 0:
            port = None

        # Note that we may pass an empty string as the host; this will throw
        # an error when we attempt to connect. Presumably, the client code
        # will call connect before then, with a proper host.
        self._setup(self._connection_class(host, port))

    def _setup(self, conn):
        self._conn = conn

        # set up delegation to flesh out interface
        self.send = conn.send
        self.putrequest = conn.putrequest
        self.endheaders = conn.endheaders
        self.set_debuglevel = conn.set_debuglevel

        conn._http_vsn = self._http_vsn
        conn._http_vsn_str = self._http_vsn_str

        self.file = None

    def connect(self, host=None, port=None):
        "Accept arguments to set the host/port, since the superclass doesn't."

        if host is not None:
            self._conn._set_hostport(host, port)
        self._conn.connect()

    def getfile(self):
        "Provide a getfile, since the superclass' does not use this concept."
        return self.file

    def putheader(self, header, *values):
        "The superclass allows only one value argument."
        self._conn.putheader(header, string.join(values, '\r\n\t'))

    def getreply(self):
        """Compat definition since superclass does not define it.

        Returns a tuple consisting of:
        - server status code (e.g. '200' if all goes well)
        - server "reason" corresponding to status code
        - any RFC822 headers in the response from the server
        """
        try:
            response = self._conn.getresponse()
        except BadStatusLine as e:
            ### hmm. if getresponse() ever closes the socket on a bad request,
            ### then we are going to have problems with self.sock

            ### should we keep this behavior? do people use it?
            # keep the socket open (as a file), and return it
            self.file = self._conn.sock.makefile('rb', 0)

            # close our socket -- we want to restart after any protocol error
            self.close()

            self.headers = None
            return -1, e.line, None

        self.headers = response.msg
        self.file = response.fp
        return response.status, response.reason, response.msg

    def close(self):
        self._conn.close()

        # note that self.file == response.fp, which gets closed by the
        # superclass. just clear the object ref here.
        ### hmm. messy. if status==-1, then self.file is owned by us.
        ### well... we aren't explicitly closing, but losing this ref will
        ### do it
        self.file = None

if hasattr(socket, 'ssl'):
    class HTTPS(HTTP):
        """Compatibility with 1.5 httplib interface

        Python 1.5.2 did not have an HTTPS class, but it defined an
        interface for sending http requests that is also useful for
        https.
        """

        _connection_class = HTTPSConnection

        def __init__(self, host='', port=None, **x509):
            # provide a default host, pass the X509 cert info

            # urf. compensate for bad input.
            if port == 0:
                port = None
            self._setup(self._connection_class(*(host, port), **x509))

            # we never actually use these for anything, but we keep them
            # here for compatibility with post-1.5.2 CVS.
            self.key_file = x509.get('key_file')
            self.cert_file = x509.get('cert_file')


class HTTPException(Exception):
    pass

class NotConnected(HTTPException):
    pass

class InvalidURL(HTTPException):
    pass

class UnknownProtocol(HTTPException):
    def __init__(self, version):
        self.version = version

class UnknownTransferEncoding(HTTPException):
    pass

class IllegalKeywordArgument(HTTPException):
    pass

class UnimplementedFileMode(HTTPException):
    pass

class IncompleteRead(HTTPException):
    def __init__(self, partial):
        self.partial = partial

class ImproperConnectionState(HTTPException):
    pass

class CannotSendRequest(ImproperConnectionState):
    pass

class CannotSendHeader(ImproperConnectionState):
    pass

class ResponseNotReady(ImproperConnectionState):
    pass

class BadStatusLine(HTTPException):
    def __init__(self, line):
        self.line = line

# for backwards compatibility
error = HTTPException


#
# snarfed from httplib.py for now...
#
def test():
    """Test this module.

    The test consists of retrieving and displaying the Python
    home page, along with the error code and error string returned
    by the www.python.org server.
    """

    import sys
    import getopt
    opts, args = getopt.getopt(sys.argv[1:], 'd')
    dl = 0
    for o, a in opts:
        if o == '-d': dl = dl + 1
    host = 'www.python.org'
    selector = '/'
    if args[0:]: host = args[0]
    if args[1:]: selector = args[1]
    h = HTTP()
    h.set_debuglevel(dl)
    h.connect(host)
    h.putrequest('GET', selector)
    h.endheaders()
    status, reason, headers = h.getreply()
    print('status =', status)
    print('reason =', reason)
    print()
    if headers:
        for header in headers.headers: print(string.strip(header))
    print()
    print(h.getfile().read())

    # minimal test that code to extract host from url works
    class HTTP11(HTTP):
        _http_vsn = 11
        _http_vsn_str = 'HTTP/1.1'

    h = HTTP11('www.python.org')
    h.putrequest('GET', 'http://www.python.org/~jeremy/')
    h.endheaders()
    h.getreply()
    h.close()

    if hasattr(socket, 'ssl'):
        host = 'sourceforge.net'
        selector = '/projects/python'
        hs = HTTPS()
        hs.connect(host)
        hs.putrequest('GET', selector)
        hs.endheaders()
        status, reason, headers = hs.getreply()
        print('status =', status)
        print('reason =', reason)
        print()
        if headers:
            for header in headers.headers: print(string.strip(header))
        print()
        print(hs.getfile().read())

# Stuff used by urlsplit
MAX_CACHE_SIZE = 20
_parse_cache = {}

uses_netloc = ['ftp', 'http', 'gopher', 'nntp', 'telnet', 'wais',
               'file',
               'https', 'shttp', 'snews',
               'prospero', 'rtsp', 'rtspu', '']
uses_query = ['http', 'wais',
              'https', 'shttp',
              'gopher', 'rtsp', 'rtspu', 'sip',
              '']
uses_fragment = ['ftp', 'hdl', 'http', 'gopher', 'news', 'nntp', 'wais',
                 'https', 'shttp', 'snews',
                 'file', 'prospero', '']

# Characters valid in scheme names
scheme_chars = ('abcdefghijklmnopqrstuvwxyz'
                'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                '0123456789'
                '+-.')


def clear_cache():
    """Clear the parse cache."""
    global _parse_cache
    _parse_cache = {}

# urlsplit does not exist in python 1.5
def urlsplit(url, scheme='', allow_fragments=1):
    """Parse a URL into 5 components:
    <scheme>://<netloc>/<path>?<query>#<fragment>
    Return a 5-tuple: (scheme, netloc, path, query, fragment).
    Note that we don't break the components up in smaller bits
    (e.g. netloc is a single string) and we don't expand % escapes."""
    key = url, scheme, allow_fragments
    cached = _parse_cache.get(key, None)
    if cached:
        return cached
    if len(_parse_cache) >= MAX_CACHE_SIZE: # avoid runaway growth
        clear_cache()
    netloc = query = fragment = ''
    i = string.find(url, ':')
    if i > 0:
        if url[:i] == 'http': # optimize the common case
            scheme = string.lower(url[:i])
            url = url[i+1:]
            if url[:2] == '//':
                i = string.find(url, '/', 2)
                if i < 0:
                    i = string.find(url, '#')
                    if i < 0:
                        i = len(url)
                netloc = url[2:i]
                url = url[i:]
            if allow_fragments and '#' in url:
                url, fragment = string.split(url, '#', 1)
            if '?' in url:
                url, query = string.split(url, '?', 1)
            tuple = scheme, netloc, url, query, fragment
            _parse_cache[key] = tuple
            return tuple
        for c in url[:i]:
            if c not in scheme_chars:
                break
        else:
            scheme, url = string.lower(url[:i]), url[i+1:]
    if scheme in uses_netloc:
        if url[:2] == '//':
            i = string.find(url, '/', 2)
            if i < 0:
                i = len(url)
            netloc, url = url[2:i], url[i:]
    if allow_fragments and scheme in uses_fragment and '#' in url:
        url, fragment = string.split(url, '#', 1)
    if scheme in uses_query and '?' in url:
        url, query = string.split(url, '?', 1)
    tuple = scheme, netloc, url, query, fragment
    _parse_cache[key] = tuple
    return tuple

# startswith not provided by the string module
def startswith(s, prefix):
    return s[:len(prefix)] == prefix 

if __name__ == '__main__':
    test()
