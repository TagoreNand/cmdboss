"""
Discovery provider contract and normalized result model.

A *provider* (adapter) knows how to read a slice of external infrastructure and
return it as a normalized snapshot of Configuration Items and relationships. The
reconciliation engine is provider-agnostic: it diffs whatever a provider returns
against the CMDB and applies the changes. Adding a new source (AWS, Azure, GCP,
an on-prem scanner, an MCP tool) means writing one ``DiscoveryProvider`` subclass
and registering it — no change to the engine, the API, or storage.

External objects are keyed by ``external_id`` (a stable id in the source system,
e.g. an EC2 instance id or an ARN). Relationships reference endpoints by
``(type, external_id)``; the reconciler resolves those to CMDB ids.
"""

from __future__ import annotations

import abc

from pydantic import BaseModel, ConfigDict, Field


class DiscoveredCI(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    external_id: str
    data: dict


class NodeExternalRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    external_id: str


class DiscoveredEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    rel_type: str
    from_ref: NodeExternalRef = Field(alias="from")
    to_ref: NodeExternalRef = Field(alias="to")
    attributes: dict | None = None


class DiscoveryResult(BaseModel):
    """Normalized desired-state snapshot returned by a provider."""

    model_config = ConfigDict(extra="forbid")
    items: list[DiscoveredCI] = Field(default_factory=list)
    relationships: list[DiscoveredEdge] = Field(default_factory=list)


class DiscoveryProvider(abc.ABC):
    """Base class for all discovery adapters.

    Subclasses set a unique ``name`` and implement :meth:`discover`. They must be
    side-effect free with respect to the CMDB — they only *read* the source and
    return normalized data; all writes happen in the reconciler.

    Example skeleton for a cloud provider (illustrative)::

        class AwsProvider(DiscoveryProvider):
            name = "aws"

            async def discover(self, config: dict) -> DiscoveryResult:
                session = aioboto3.Session(profile_name=config.get("profile"))
                async with session.client("ec2", region_name=config["region"]) as ec2:
                    resp = await ec2.describe_instances()
                items, edges = [], []
                for res in resp["Reservations"]:
                    for inst in res["Instances"]:
                        items.append(DiscoveredCI(
                            type="server",
                            external_id=inst["InstanceId"],
                            data={"hostname": inst.get("PrivateDnsName", inst["InstanceId"]),
                                  "environment": _env_from_tags(inst), ...},
                        ))
                        if inst.get("VpcId"):
                            edges.append(DiscoveredEdge(rel_type="in_vpc",
                                from_ref=NodeExternalRef(type="server", external_id=inst["InstanceId"]),
                                to_ref=NodeExternalRef(type="vpc", external_id=inst["VpcId"])))
                return DiscoveryResult(items=items, relationships=edges)
    """

    name: str = "base"

    @abc.abstractmethod
    async def discover(self, config: dict) -> DiscoveryResult:
        """Return the desired-state snapshot for the given run configuration."""
        raise NotImplementedError
