import math
import os
import sys
import argparse
import requests
from time import sleep as _sleep

# Every HTTP call has a timeout: without one a wedged Striim blocks the caller forever (a live
# teardown once sat 9m30s in stop_application until killed, never reaching its forced drop).
# (connect, read) seconds. The read timeout is long because state-changing commands -- a large
# TQL deploy, DROP NAMESPACE ... CASCADE, LOAD OPEN PROCESSOR -- are legitimately slow, and a
# client that gives up early leaves the server still running the command. A caller with its own
# budget passes `timeout=` to the method (seconds, or a (connect, read) pair; a None element means
# that half of the default), or NO_TIMEOUT for a call that must never be abandoned.
# STRIIM_API_TIMEOUT overrides the default: "600" (read; connect stays at most 10), "10,600", or
# "0" for no timeout on any call, per-call ones included; it is read once, when the client is
# created.
DEFAULT_TIMEOUT = (10, 600)


class _NoTimeout:
    # A distinct object, not 0: a caller's budget that runs down to 0 must not mean "forever".
    def __repr__(self):
        return "NO_TIMEOUT"


NO_TIMEOUT = _NoTimeout()
_UNSET = object()


def parse_timeout(raw):
    """A STRIIM_API_TIMEOUT value -> the default timeout: a (connect, read) pair, or None for no
    timeout. Raises ValueError, naming the setting, on a malformed value."""
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        parts = [float(p) for p in raw.split(",")]
    except ValueError:
        parts = []
    if len(parts) not in (1, 2) or any(not math.isfinite(p) or p < 0 for p in parts):
        raise ValueError(f"STRIIM_API_TIMEOUT={raw!r}: expected seconds, as \"600\", \"10,600\" or "
                         f"\"0\" for no timeout")
    if parts == [0]:
        return None
    if any(p == 0 for p in parts):
        raise ValueError(f"STRIIM_API_TIMEOUT={raw!r}: use \"0\" alone for no timeout")
    if len(parts) == 1:
        return (min(DEFAULT_TIMEOUT[0], parts[0]), parts[0])   # an unreachable host still fails fast
    return (parts[0], parts[1])


def read_timed_out(e):
    """True when the request reached the server and the read timed out, so the server may still
    be running the command. requests raises ReadTimeout while waiting for the headers but a
    ConnectionError wrapping urllib3's ReadTimeoutError while reading the body."""
    import urllib3
    if isinstance(e, requests.exceptions.ReadTimeout):
        return True
    return isinstance(e, requests.exceptions.ConnectionError) and any(
        isinstance(a, urllib3.exceptions.ReadTimeoutError) for a in e.args)


def _timeout():
    return parse_timeout(os.environ.get("STRIIM_API_TIMEOUT", ""))


class StriimApi:

    def __init__(self, host, port, username, password, login_timeout=None):
      self.url_base = "http://" + host + ":" + str(port)
      self.username = username
      self.password = password
      self.default_timeout = _timeout()   # STRIIM_API_TIMEOUT, validated now rather than mid-call
      self.getAuthToken(timeout=login_timeout)

    # An instance made without __init__ (some tests do) reads STRIIM_API_TIMEOUT on first use.
    default_timeout = _UNSET

    def _t(self, timeout):
      # None means the client's default. A per-call NO_TIMEOUT means no timeout, and so does
      # STRIIM_API_TIMEOUT=0 for every call: the escape hatch for a cluster too slow for the
      # callers' own shorter timeouts. Any other per-call timeout is capped at the default, so
      # a STRIIM_API_TIMEOUT set to fail fast is never exceeded.
      if self.default_timeout is _UNSET:
        self.default_timeout = _timeout()
      default = self.default_timeout
      if timeout is NO_TIMEOUT:
        return None
      if timeout is None:
        return default
      pair = tuple(timeout) if isinstance(timeout, (tuple, list)) else (timeout, timeout)
      if len(pair) != 2:
        raise ValueError(f"timeout={timeout!r}: expected seconds or a (connect, read) pair")
      for value in pair:   # validated whatever STRIIM_API_TIMEOUT says, so behaviour is the same everywhere
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not value > 0):
          raise ValueError(f"timeout={timeout!r}: seconds must be > 0; use NO_TIMEOUT for none")
      if default is None:
        return None
      return tuple(cap if value is None else min(value, cap) for value, cap in zip(pair, default))

    # Function to Generate Striim Access Token
    def getAuthToken(self, timeout=None):
      url = self.url_base  + "/security/authenticate"
      try:
        response = requests.post(
          url,
          data={'username': self.username,'password': self.password},
          timeout=self._t(timeout)
        )
        response.raise_for_status()
      except requests.exceptions.HTTPError as err:
        # An Exception, not SystemExit: callers report a failed login and carry on or skip.
        raise RuntimeError(f"Striim authentication failed: {err}") from err
      self.auth_token = response.json()['token']

    # Function to Generate API Call Headers
    def getHeader(self, content_type):
      headers = {
        'authorization': 'STRIIM-TOKEN ' + str(self.auth_token),
        'content-type': content_type
      }
      return headers

    # Function To Deploy Application
    def deploy_application(self, application_name, timeout=None):
      url = self.url_base  + "/api/v2/applications/" + application_name + "/deployment"
      try:
        headers = self.getHeader('application/json')
        response = requests.post(
          url,
          headers=headers,
          data='{"deploymentGroupName": "default","deploymentType": "ANY", "flows": []}',
          timeout=self._t(timeout)
        )
        if response.status_code == 200:
          print("Application deployed successfully")
        else:
          print("Application deployed failed")
          response.raise_for_status()
      except requests.exceptions.HTTPError as err:
        raise SystemExit(err)

    #Function to start Striim Application
    def start_application(self, application_name, timeout=None):
      url = self.url_base  + "/api/v2/applications/" + application_name + "/sprint"
      try:
        headers = self.getHeader('application/json')
        response = requests.post(url, headers=headers, timeout=self._t(timeout))
        print("start_application response {} ".format(response))
      except requests.exceptions.HTTPError as err:
        raise SystemExit(err)

    # Function to Stop Application
    def stop_application(self, application_name, timeout=None):
      url = self.url_base  + "/api/v2/applications/" + application_name + "/sprint"
      try:
        headers = self.getHeader('application/json')
        response = requests.delete(url, headers=headers, timeout=self._t(timeout))
        print("stop_application response {} ".format(response))
      except requests.exceptions.HTTPError as err:
        raise SystemExit(err)

    # Function to undeploy application
    def undeploy_application(self, application_name, timeout=None):
      url = self.url_base  + "/api/v2/applications/" + application_name + "/deployment"
      try:
        retry = 0
        while retry < 3:
          status = self.status_application(application_name, timeout=timeout)
          retry = retry + 1
          if status == "STOPPING":
            print("Still Stopping - Waiting for 20 Secs")
            _sleep(20)
          else:
            break
        headers = self.getHeader('application/json')
        response = requests.delete(url, headers=headers, timeout=self._t(timeout))
        print("undeploy_application response {} ".format(response))
      except requests.exceptions.HTTPError as err:
        raise SystemExit(err)

    # Function to get status of application
    def status_application(self, application_name, timeout=None):
      url = self.url_base  + "/api/v2/applications/" + application_name
      try:
        headers = self.getHeader('application/json')
        response = requests.get(url, headers=headers, timeout=self._t(timeout))
        # Do NOT go straight to response.json()['status']. requests does not raise on 4xx/5xx
        # without raise_for_status(), so the except clause below is dead for HTTP errors: a 404
        # ("Application not found", body {"message": ...}) used to reach the subscript and
        # surface as a bare KeyError('status') with the response discarded. Every distinguishable
        # failure -- wrong app name, 404, 500, auth, shape drift, a cluster mid-restart --
        # collapsed into one identical uninformative error. Report the code and the body instead.
        if response.status_code != 200:
            raise RuntimeError(
                "status_application({}) -> HTTP {}: {}".format(
                    application_name, response.status_code, response.text[:300]))
        body = response.json()
        if 'status' not in body:
            raise RuntimeError(
                "status_application({}) -> HTTP 200 but no 'status' key; keys={}; body={}".format(
                    application_name, sorted(body) if isinstance(body, dict) else type(body).__name__,
                    response.text[:300]))
        status = body['status']
        print("Current application status = {}".format(status))
        return status
      except requests.exceptions.HTTPError as errval:
        raise SystemExit(errval)

    # Function to post a tungsten file
    def post_tungsten_file(self, file_name, timeout=None):
      url = self.url_base  + "/api/v2/tungsten"
      try:
        headers = self.getHeader('text/plain')
        with open(file_name, 'r') as f:
          data = f.read()
        response = requests.post(url, headers=headers, data=data, timeout=self._t(timeout))
        status = response.json()
        for s in status:
          print("Tungsten status = {}".format(s))
        return status
      except requests.exceptions.HTTPError as errval:
        raise SystemExit(errval)

    # Function to post a tungsten line
    def post_tungsten_line(self, line, timeout=None):
        url = self.url_base  + "/api/v2/tungsten"
        try:
          headers = self.getHeader('text/plain')
          response = requests.post(url, headers=headers, data=line, timeout=self._t(timeout))
          status = response.json()
          for s in status:
            print("Tungsten status = {}".format(s))
          return status
        except requests.exceptions.HTTPError as errval:
          raise SystemExit(errval)

def parse_arguments():
    parser = argparse.ArgumentParser(description="Manage Striim applications.")
    parser.add_argument("action", choices=["deploy", "start", "stop", "undeploy", "status", "tungsten_file", "tungsten_line"],
                        help="Action to perform on the application or file.")
    parser.add_argument("name", help="Application name or file name.")
    parser.add_argument("--host", default="localhost", help="Host of the Striim server (default: localhost).")
    parser.add_argument("--port", type=int, default=9080, help="Port of the Striim server (default: 9080).")
    parser.add_argument("--username", default="admin", help="Username for Striim (default: admin).")
    parser.add_argument("--password", default="striim", help="Password for Striim (default: striim).")

    args = parser.parse_args()

    # Sanity checks
    if args.port < 1 or args.port > 65535:
        print("Error: Port must be between 1 and 65535.")
        sys.exit(1)

    return args

def _main(args):
    # Initialize the API with parsed arguments
    api = StriimApi(args.host, args.port, args.username, args.password)

    # Execute the corresponding action
    if args.action == "deploy":
        api.deploy_application(args.name)
    elif args.action == "start":
        api.start_application(args.name)
    elif args.action == "stop":
        api.stop_application(args.name)
    elif args.action == "undeploy":
        api.undeploy_application(args.name)
    elif args.action == "status":
        api.status_application(args.name)
    elif args.action == "tungsten_file":
        api.post_tungsten_file(args.name)
    elif args.action == "tungsten_line":
        api.post_tungsten_line(args.name)
    else:
        print("Invalid action specified.")

#Main Function
if __name__ == "__main__":
    try:
        _main(parse_arguments())
    except requests.exceptions.ConnectTimeout as e:
        sys.exit(f"Could not connect to Striim ({e}); check the host and port.")
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        if not read_timed_out(e):
            raise
        sys.exit(f"Striim did not answer in time ({e}); the server may still be running the "
                 f"command. STRIIM_API_TIMEOUT sets the default timeout.")
    except ValueError as e:
        if "STRIIM_API_TIMEOUT" not in str(e):
            raise
        sys.exit(str(e))
