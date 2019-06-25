#!/urs/bin/env python3
# osiris-build-observer: OpenShift build event observer.

# Copyright(C) 2019 Marek Cermak
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""Observer module.

This module observes OpenShift namespace and watches for build events.
When such event occurs, it puts it to [Osiris](https://github.com/thoth-station/osiris)
endpoint for further processing.
"""

import os
import re
import sys
import typing

import requests
import urllib3

import logging

from functools import reduce

from http import HTTPStatus
from pathlib import Path
from requests.adapters import HTTPAdapter
from requests.adapters import Retry
from urllib.parse import urljoin

from openshift.dynamic import ResourceInstance

from thoth.common import OpenShift
from thoth.common import init_logging


init_logging({"thoth.osiris_build_observer": os.getenv("LOG_LEVEL", "INFO")})

_LOGGER = logging.getLogger("thoth.osiris_build_observer")


urllib3.disable_warnings(category=urllib3.exceptions.InsecureRequestWarning)

# osiris configuration

_OSIRIS_HOST_NAME = os.getenv("OSIRIS_HOST_NAME", "http://0.0.0.0")
_OSIRIS_HOST_PORT = os.getenv("OSIRIS_HOST_PORT", "8000")

_OSIRIS_BUILD_LOGS_URL = "build/logs/"
_OSIRIS_BUILD_START_HOOK = "build/started/"
_OSIRIS_BUILD_COMPLETED_HOOK = "build/completed/"

# openshift client

_NAMESPACE_FILENAME = "/run/secrets/kubernetes.io/serviceaccount/namespace"

try:
    _NAMESPACE = Path(_NAMESPACE_FILENAME).read_text()
except FileNotFoundError:
    _NAMESPACE = os.getenv("MIDDLETIER_NAMESPACE", None)

_OPENSHIFT_CLIENT = OpenShift(middletier_namespace=_NAMESPACE)


def noexcept(fun: typing.Callable):
    """Decorate non-throwing function."""

    def _inner(*args, **kwargs):

        ret = None
        # noinspection PyBroadException
        try:
            ret = fun(*args, **kwargs)
        except Exception as exc:
            # TODO: log caught exception warnging
            print("[WARNING] Exception caught:", exc, file=sys.stderr)

        return ret

    return _inner


class RetrySession(requests.Session):
    """RetrySession class.

    RetrySession attempts to retry failed requests and timeouts
    and holds the state between requests. Request periods are
    progressively prolonged for a certain amount of retries.
    """

    _REQUEST_BACKOFF_FACTOR = 60  # determines sleep time
    _REQUESTS_MAX_RETRIES = 5

    def __init__(self, adapter_prefixes: typing.List[str] = None, method_whitelist: typing.List[str] = None):
        """Initialize RetrySession."""
        super(RetrySession, self).__init__()

        adapter_prefixes = adapter_prefixes or ["http://", "https://"]

        retry_config = Retry(
            total=self._REQUESTS_MAX_RETRIES,
            connect=self._REQUESTS_MAX_RETRIES,
            backoff_factor=self._REQUEST_BACKOFF_FACTOR,
            method_whitelist=method_whitelist,
        )
        retry_adapter = HTTPAdapter(max_retries=retry_config)

        for prefix in adapter_prefixes:
            self.mount(prefix, retry_adapter)

    def send_request(self, request):
        """Send request and return whether it was successful."""
        resp = self.send(request, timeout=60)

        success = resp.status_code in (HTTPStatus.ACCEPTED, HTTPStatus.CREATED, HTTPStatus.OK)

        if success:
            _LOGGER.info("Success.")
        else:
            _LOGGER.info("Failure.")

        _LOGGER.debug("Status: %d  Reason: %r  Response: %s", resp.status_code, resp.reason, resp.json())

        return success


if __name__ == "__main__":

    v1_build = _OPENSHIFT_CLIENT.ocp_client.resources.get(api_version="v1", kind="Build")
    base_url = ":".join([_OSIRIS_HOST_NAME, _OSIRIS_HOST_PORT])
    put_request = requests.Request(
        url=base_url,
        method="PUT",
        headers={"content-type": "application/json"},
        params={"mode": "cluster"},
    )

    with RetrySession() as r3_session:

        _LOGGER.info(f"Watching for build events in {_NAMESPACE} namespace ...")

        for streamed_event in v1_build.watch(namespace=_NAMESPACE):

            kube_event: ResourceInstance = streamed_event["object"]
            kube_event_raw: dict = streamed_event["raw_object"]

            _LOGGER.debug("New event received.")
            _LOGGER.debug("Kind: %s", kube_event.kind)
            _LOGGER.debug("Config: \n%s", kube_event.status.config)

            build_id = kube_event.metadata.name
            build_complete = re.search(r"Complete", kube_event.status.phase, re.IGNORECASE)

            build_log = {
                "data": None,
                "metadata": {
                    # TODO: more useful metadata?
                    "build_id": build_id
                },
            }

            if build_complete:

                try:
                    build_log["data"] = _OPENSHIFT_CLIENT.get_build_log(build_id=build_id, namespace=_NAMESPACE)
                except requests.exceptions.HTTPError as exc:
                    _LOGGER.debug("OpenShift master response for build log (%d): %r", exc)

                osiris_endpoint = _OSIRIS_BUILD_COMPLETED_HOOK

            else:
                osiris_endpoint = _OSIRIS_BUILD_START_HOOK

            put_request.url = reduce(urljoin, [base_url, osiris_endpoint, build_id])
            put_request.json = kube_event_raw

            build_info_request = r3_session.prepare_request(put_request)

            _LOGGER.info("Posting event '%s' to: %s", kube_event.kind, put_request.url)

            successful = r3_session.send_request(build_info_request)

            if build_complete and successful:
                put_request.url = reduce(urljoin, [base_url, _OSIRIS_BUILD_LOGS_URL, build_id])
                put_request.json = build_log
                build_log_request = r3_session.prepare_request(put_request)

                _LOGGER.info("Posting build log to: %s", put_request.url)

                # TODO: Osiris does not allow to put build log and build info in one request
                r3_session.send_request(build_log_request)
