#!/usr/bin/env python
#
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import functools
import logging
import os
from typing import Any, Callable, List, TypeVar, cast
from uuid import UUID

from azure.core.exceptions import ClientAuthenticationError
from azure.graphrbac import GraphRbacManagementClient
from azure.graphrbac.models import CheckGroupMembershipParameters
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.subscription import SubscriptionClient
from memoization import cached
from msrestazure.azure_active_directory import MSIAuthentication
from msrestazure.tools import parse_resource_id
from onefuzztypes.primitives import Container, Region

from .monkeypatch import allow_more_workers, reduce_logging


@cached
def get_msi() -> MSIAuthentication:
    allow_more_workers()
    reduce_logging()
    return MSIAuthentication()


@cached
def get_identity() -> DefaultAzureCredential:
    allow_more_workers()
    reduce_logging()
    return DefaultAzureCredential()


@cached
def get_base_resource_group() -> Any:  # should be str
    return parse_resource_id(os.environ["ONEFUZZ_RESOURCE_GROUP"])["resource_group"]


@cached
def get_base_region() -> Region:
    client = ResourceManagementClient(
        credential=get_identity(), subscription_id=get_subscription()
    )
    group = client.resource_groups.get(get_base_resource_group())
    return Region(group.location)


@cached
def get_subscription() -> Any:  # should be str
    return parse_resource_id(os.environ["ONEFUZZ_DATA_STORAGE"])["subscription"]


@cached
def get_insights_instrumentation_key() -> Any:  # should be str
    return os.environ["APPINSIGHTS_INSTRUMENTATIONKEY"]


@cached
def get_insights_appid() -> str:
    return os.environ["APPINSIGHTS_APPID"]


@cached
def get_instance_name() -> str:
    return os.environ["ONEFUZZ_INSTANCE_NAME"]


@cached
def get_instance_url() -> str:
    return "https://%s.azurewebsites.net" % get_instance_name()


@cached
def get_instance_id() -> UUID:
    from .containers import get_blob
    from .storage import StorageType

    blob = get_blob(Container("base-config"), "instance_id", StorageType.config)
    if blob is None:
        raise Exception("missing instance_id")
    return UUID(blob.decode())


DAY_IN_SECONDS = 60 * 60 * 24


@cached(ttl=DAY_IN_SECONDS)
def get_regions() -> List[Region]:
    subscription = get_subscription()
    client = SubscriptionClient(credential=get_identity())
    locations = client.subscriptions.list_locations(subscription)
    return sorted([Region(x.name) for x in locations])


@cached
def get_graph_client() -> GraphRbacManagementClient:
    return GraphRbacManagementClient(get_msi(), get_subscription())


def is_member_of(group_id: str, member_id: str) -> bool:
    client = get_graph_client()
    return bool(
        client.groups.is_member_of(
            CheckGroupMembershipParameters(group_id=group_id, member_id=member_id)
        ).value
    )


@cached
def get_scaleset_identity_resource_path() -> str:
    scaleset_id_name = "%s-scalesetid" % get_instance_name()
    resource_group_path = "/subscriptions/%s/resourceGroups/%s/providers" % (
        get_subscription(),
        get_base_resource_group(),
    )
    return "%s/Microsoft.ManagedIdentity/userAssignedIdentities/%s" % (
        resource_group_path,
        scaleset_id_name,
    )


@cached
def get_scaleset_principal_id() -> UUID:
    api_version = "2018-11-30"  # matches the apiversion in the deployment template
    client = ResourceManagementClient(
        credential=get_identity(), subscription_id=get_subscription()
    )
    uid = client.resources.get_by_id(get_scaleset_identity_resource_path(), api_version)
    return UUID(uid.properties["principalId"])


@cached
def get_keyvault_client(vault_url: str) -> SecretClient:
    return SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())


def clear_azure_client_cache() -> None:
    # clears the memoization of the Azure clients.

    from .compute import get_compute_client
    from .containers import get_blob_service
    from .network_mgmt_client import get_network_client
    from .storage import get_mgmt_client

    # currently memoization.cache does not project the wrapped function's types.
    # As a workaround, CI comments out the `cached` wrapper, then runs the type
    # validation.  This enables calling the wrapper's clear_cache if it's not
    # disabled.
    for func in [
        get_msi,
        get_identity,
        get_compute_client,
        get_blob_service,
        get_network_client,
        get_mgmt_client,
    ]:
        clear_func = getattr(func, "clear_cache", None)
        if clear_func is not None:
            clear_func()


T = TypeVar("T", bound=Callable[..., Any])


class retry_on_auth_failure:
    def __call__(self, func: T) -> T:
        @functools.wraps(func)
        def decorated(*args, **kwargs):  # type: ignore
            try:
                return func(*args, **kwargs)
            except ClientAuthenticationError as err:
                logging.warning(
                    "clearing authentication cache after auth failure: %s", err
                )

            clear_azure_client_cache()
            return func(*args, **kwargs)

        return cast(T, decorated)
