#!/usr/bin/env vpython
# Copyright (c) 2020 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import contextlib
import json
import os
import requests
import time

# Constants describing TestStatus for ResultDB
STATUS_PASS = 'PASS'
STATUS_FAIL = 'FAIL'
STATUS_CRASH = 'CRASH'
STATUS_ABORT = 'ABORT'
STATUS_SKIP = 'SKIP'


class ResultSink(object):
  def __init__(self, session, url, test_id_prefix):
    self._session = session
    self._url = url
    self._test_id_prefix = test_id_prefix

  def report(self, function_name, status, elapsed_time):
    """Reports the result and elapsed time of a presubmit function call.

    Args:
      function_name (str): The name of the presubmit function
      status: the status to report the function call with
      elapsed_time: the time taken to invoke the presubmit function
    """
    tr = {
        'testId': self._test_id_prefix + function_name,
        'status': status,
        'expected': status == STATUS_PASS,
        'duration': '{:.9f}s'.format(elapsed_time)
    }
    self._session.post(self._url, json={'testResults': [tr]})


@contextlib.contextmanager
def client(test_id_prefix):
  """Returns a client for ResultSink.

  This is a context manager that returns a client of ResultSink, if LUCI_CONTEXT
  with a section of result_sink is present. Otherwise, this returns None.

  Args:
    test_id_prefix: A prefix to be added to the test ID of reported function
      names.

  Returns:
    An instance of ResultSink() if the luci context is present. None, otherwise.
  """
  luci_ctx = os.environ.get('LUCI_CONTEXT')
  if not luci_ctx:
    yield None
    return

  sink_ctx = None
  with open(luci_ctx) as f:
    sink_ctx = json.load(f).get('result_sink')
    if not sink_ctx:
      yield None
      return

  url='http://{0}/prpc/luci.resultsink.v1.Sink/ReportTestResults'.format(
      sink_ctx['address'])
  with requests.Session() as s:
    s.headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Authorization': 'ResultSink {0}'.format(sink_ctx['auth_token'])
    }
    yield ResultSink(s, url, test_id_prefix)
