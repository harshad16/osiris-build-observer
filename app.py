#!/bin/env python
# Osiris: Build log aggregator.

"""Observer module.

This module observer OpenShift namespace and watches for build events.
When such event occur, it puts it to [Osiris](https://github.com/CermakM/osiris)
endpoint for further processing.
"""

import os
import re
import time
import typing

import requests
import urllib3

import daiquiri
import logging

from functools import reduce

from http import HTTPStatus
from pathlib import Path
from requests.adapters import HTTPAdapter
from requests.adapters import Retry
from urllib.parse import urljoin

import kubernetes
from kubernetes.client.models.v1_event import V1Event as Event

from osiris.utils import noexcept
from osiris.schema.auth import Login, LoginSchema
from osiris.schema.build import BuildInfo, BuildInfoSchema


daiquiri.setup(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO'), logging.INFO)
)

urllib3.disable_warnings(category=urllib3.exceptions.InsecureRequestWarning)


_LOGGER = daiquiri.getLogger()

# osiris configuration

_OSIRIS_HOST_NAME = os.getenv("OSIRIS_HOST_NAME", "http://0.0.0.0")
_OSIRIS_HOST_PORT = os.getenv("OSIRIS_HOST_PORT", "5000")
_OSIRIS_LOGIN_ENDPOINT = "/auth/login"
_OSIRIS_BUILD_START_HOOK = "/build/started/"
_OSIRIS_BUILD_COMPLETED_HOOK = "/build/completed/"

# oc namespace

_NAMESPACE_FILENAME = '/run/secrets/kubernetes.io/serviceaccount/namespace'

try:
    _NAMESPACE = Path(_NAMESPACE_FILENAME).read_text()
except FileNotFoundError:
    _NAMESPACE = os.getenv("OC_NAMESPACE", None)

_REQUESTS_MAX_RETRIES = 10


# kubernetes configuration

_SERVICE_TOKEN_FILENAME = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_SERVICE_CERT_FILENAME = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

if Path(_SERVICE_TOKEN_FILENAME).exists():  # in-cluster configuration

    _KUBE_CONFIG = kubernetes.config.incluster_config.InClusterConfigLoader(
        token_filename=_SERVICE_TOKEN_FILENAME, cert_filename=_SERVICE_CERT_FILENAME)
    _KUBE_CONFIG.load_and_set()

    _KUBE_CLIENT = kubernetes.client.CoreV1Api()
    _KUBE_API = _KUBE_CLIENT.api_client

else:  # default configuration, assumes current host logs in to the oc cluster by himself
    _KUBE_CONFIG = kubernetes.client.Configuration()
    _KUBE_CONFIG.host = os.getenv("OC_HOST_NAME", 'localhost')
    _KUBE_CONFIG.verify_ssl = False

    kubernetes.config.load_kube_config(client_configuration=_KUBE_CONFIG)

    _KUBE_API = kubernetes.client.ApiClient(_KUBE_CONFIG)
    _KUBE_CLIENT = kubernetes.client.CoreV1Api(_KUBE_API)


class RetrySession(requests.Session):

    """RetrySession class.

    RetrySession attempts to retry failed requests and timeouts
    and holds the state between requests. Request periods are
    progressively prolonged for a certain amount of retries.
    """

    def __init__(self,
                 adapter_prefixes: typing.List[str] = None,
                 status_forcelist: typing.Tuple[int] = (500, 502, 504),
                 method_whitelist: typing.List[str] = None):

        super(RetrySession, self).__init__()

        adapter_prefixes = adapter_prefixes or ["http://", "https://"]

        retry_config = Retry(
            total=_REQUESTS_MAX_RETRIES,
            connect=_REQUESTS_MAX_RETRIES,
            backoff_factor=5,  # determines sleep time
            status_forcelist=status_forcelist,
            method_whitelist=method_whitelist
        )
        retry_adapter = HTTPAdapter(max_retries=retry_config)

        for prefix in adapter_prefixes:
            self.mount(prefix, retry_adapter)


def _authenticate(session: requests.Session, server: str, token: str):
    """Authenticate the Osiris API to the cluster with current credentials."""
    login_schema = LoginSchema()
    logging_prefix = "[AUTHENTICATION]"

    login = Login(
        server=server,
        token=token
    )
    login_data, _ = login_schema.dump(login)

    post_request = requests.Request(
        method='POST',
        url=urljoin(':'.join([_OSIRIS_HOST_NAME, _OSIRIS_HOST_PORT]),
                    _OSIRIS_LOGIN_ENDPOINT),
        headers={'content-type': 'application/json'},
        json=login_data
    )
    auth_request = session.prepare_request(post_request)

    _LOGGER.info("%s Authenticating.", logging_prefix)

    login_resp = session.send(auth_request, timeout=60)

    if login_resp.status_code == HTTPStatus.ACCEPTED:

        _LOGGER.info("%s, Success.", logging_prefix)

    else:

        _LOGGER.info("%s Failure.", logging_prefix)
        _LOGGER.info("%s Status: %d  Reason: %r",
                     logging_prefix, login_resp.status_code, login_resp.reason)

    _LOGGER.debug("%s Status: %d  Reason: %r  Response: %r",
                  logging_prefix, login_resp.reason, login_resp.status_code, login_resp.json)

    return login_resp


@noexcept
def _is_pod_event(event: Event) -> bool:
    return event.involved_object.kind == 'Pod'


@noexcept
def _is_build_event(event: Event) -> bool:
    is_build = event.involved_object.kind == 'Build'
    # check for [BuildStarted, BuildCompleted, BuildFailed, ...] events
    is_valid = re.search(r'^Build', event.reason, flags=re.IGNORECASE)

    return is_build and is_valid


@noexcept
def _is_observed_event(event: Event) -> bool:
    # TODO: check for valid event names
    return _is_build_event(event) or _is_pod_event(event)


if __name__ == "__main__":

    watch = kubernetes.watch.Watch()

    with RetrySession() as r3_session:

        # authenticate osiris api
        _authenticate(
            session=r3_session,
            server=getattr(_KUBE_CONFIG, 'host', None) or os.getenv('OC_HOST_NAME', None),
            token=getattr(_KUBE_CONFIG, 'token', None) or os.getenv('OC_TOKEN', None)
        )

        put_request = requests.Request(
                url=':'.join([_OSIRIS_HOST_NAME, _OSIRIS_HOST_PORT]),
                method='PUT',
                headers={'content-type': 'application/json'}
        )

        _LOGGER.info(f"Watching for events in {_NAMESPACE} namespace ...")

        for streamed_event in watch.stream(_KUBE_CLIENT.list_namespaced_event,
                                           namespace=_NAMESPACE):

            kube_event: Event = streamed_event['object']

            if not _is_observed_event(kube_event):

                time.sleep(5)  # there are probably no events atm so no need to process quickly
                continue

            _LOGGER.debug("[EVENT] New event received.")
            _LOGGER.debug("[EVENT] Reason: %s", kube_event.reason)
            _LOGGER.debug("[EVENT] Involved object: %s", kube_event.involved_object)

            if _is_pod_event(kube_event):
                continue

            # get associated pod and use it as a build_id
            build_info = BuildInfo.from_event(kube_event)
            build_url = urljoin(_KUBE_CLIENT.api_client.configuration.host,
                                build_info.ocp_info.self_link),

            schema = BuildInfoSchema()
            data, errors = schema.dump(build_info)

            osiris_endpoint = _OSIRIS_BUILD_COMPLETED_HOOK if build_info.build_complete() else _OSIRIS_BUILD_START_HOOK

            put_request.url = reduce(urljoin, [put_request.url, osiris_endpoint, build_info.build_id])
            put_request.json = data

            prep_request = r3_session.prepare_request(put_request)

            dry_run_prefix = "[DRY-RUN] " if os.getenv("DRY_RUN", 'false') == 'true' else ""

            _LOGGER.debug("%s[EVENT] Event to be posted: %r", dry_run_prefix, kube_event)
            _LOGGER.debug("%s[EVENT] Request: %r", dry_run_prefix, prep_request)

            _LOGGER.info("%s[EVENT] Posting event '%s' to: %s", dry_run_prefix, kube_event.kind, put_request.url)

            if not dry_run_prefix:

                try:
                    resp = r3_session.send(prep_request, timeout=60)

                except urllib3.exceptions.MaxRetryError:

                    _LOGGER.error("[EVENT] Failure. Max retries exceeded. Skipping.")

                else:

                    if resp.status_code == HTTPStatus.ACCEPTED:

                        _LOGGER.info("[EVENT] Success.")

                    else:

                        _LOGGER.info("[EVENT] Failure.")
                        _LOGGER.info("[EVENT] Status: %d  Reason: %r",
                                     resp.status_code, resp.reason)

                    _LOGGER.debug("[EVENT] Status: %d  Reason: %r  Response: %r",
                                  resp.status_code, resp.reason, resp.json)

            else:

                _LOGGER.info("%sFinished.", dry_run_prefix)
