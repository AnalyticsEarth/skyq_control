from __future__ import annotations

from collections.abc import AsyncIterator

import aiohttp
import pytest

from aioskyq import EpgClient, SkyQBox

from .fakebox import FakeSkyQ


@pytest.fixture(params=["gateway", "mini"])
async def fake(request: pytest.FixtureRequest) -> AsyncIterator[FakeSkyQ]:
    box = await FakeSkyQ(request.param).start()
    yield box
    await box.stop()


@pytest.fixture
async def gateway() -> AsyncIterator[FakeSkyQ]:
    box = await FakeSkyQ("gateway").start()
    yield box
    await box.stop()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


def make_client(box: FakeSkyQ, session: aiohttp.ClientSession) -> SkyQBox:
    return SkyQBox(
        "127.0.0.1",
        session,
        rest_port=box.rest_port,
        upnp_port=box.upnp_port,
        remote_port=box.remote_port,
        key_gap=0.01,
        epg_client=EpgClient(session, hawk_base=box.hawk_base, atlantis_base=box.atlantis_base),
    )


@pytest.fixture
async def client(fake: FakeSkyQ, session: aiohttp.ClientSession) -> AsyncIterator[SkyQBox]:
    box = make_client(fake, session)
    yield box
    await box.close()


@pytest.fixture
async def gw_client(gateway: FakeSkyQ, session: aiohttp.ClientSession) -> AsyncIterator[SkyQBox]:
    box = make_client(gateway, session)
    yield box
    await box.close()
