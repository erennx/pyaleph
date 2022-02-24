""" Storage module for Aleph.
Basically manages the IPFS storage.
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Any, AnyStr, Dict, IO, Optional

from aleph.exceptions import InvalidContent, ContentCurrentlyUnavailable
from aleph.services.filestore import get_value, set_value
from aleph.services.ipfs.storage import add_bytes as add_ipfs_bytes
from aleph.services.ipfs.storage import add_file as ipfs_add_file
from aleph.services.ipfs.storage import get_ipfs_content
from aleph.services.ipfs.storage import pin_add as ipfs_pin_add
from aleph.services.p2p.http import request_hash as p2p_http_request_hash
from aleph.services.p2p.singleton import get_streamer
from aleph.types import ItemType
from aleph.utils import run_in_executor, get_sha256
from aleph.web import app

LOGGER = logging.getLogger("STORAGE")


class ContentSource(str, Enum):
    DB = "DB"
    P2P = "P2P"
    IPFS = "IPFS"
    INLINE = "inline"


@dataclass
class StoredContent:
    hash: str
    source: Optional[ContentSource]


@dataclass
class RawContent(StoredContent):
    value: bytes

    def __len__(self):
        return len(self.value)


@dataclass
class JsonContent(StoredContent):
    value: Any
    raw_content: bytes


async def json_async_loads(s: AnyStr):
    """Deserialize ``s`` (a ``str``, ``bytes`` or ``bytearray`` instance
    containing a JSON document) to a Python object in an asynchronous executor."""
    return await run_in_executor(None, json.loads, s)


async def get_message_content(message: Dict) -> JsonContent:
    item_type: str = message.get("item_type", ItemType.IPFS)
    item_hash = message["item_hash"]

    if item_type in (ItemType.IPFS, ItemType.Storage):
        return await get_json(item_hash, engine=ItemType(item_type))
    elif item_type == ItemType.Inline:
        if "item_content" not in message:
            error_msg = f"No item_content in message {message.get('item_hash')}"
            LOGGER.warning(error_msg)
            raise InvalidContent(error_msg)  # never retry, bogus data
        try:
            item_content = await json_async_loads(message["item_content"])
        except (json.JSONDecodeError, KeyError) as e:
            error_msg = f"Can't decode JSON: {e}"
            LOGGER.exception(error_msg)
            raise InvalidContent(error_msg)

        return JsonContent(
            hash=item_hash,
            source=ContentSource.INLINE,
            value=item_content,
            raw_content=message["item_content"],
        )
    else:
        # unknown, could retry later? shouldn't have arrived this far though.
        error_msg = f"Unknown item type: '{item_type}'."
        raise ContentCurrentlyUnavailable(error_msg)


async def fetch_content_from_network(
    content_hash: str, engine: ItemType, timeout: int
) -> Optional[bytes]:
    content = None
    enabled_clients = app["config"].p2p.clients.value

    if "protocol" in enabled_clients:
        streamer = get_streamer()
        content = await streamer.request_hash(content_hash)

    if content is None and "http" in enabled_clients:
        content = await p2p_http_request_hash(content_hash, timeout=timeout)

    if content is not None:
        await verify_content_hash(content, engine, content_hash)

    return content


async def compute_content_hash_ipfs(
    content: bytes, expected_hash: str
) -> Optional[str]:
    """
    Computes the IPFS hash of the content.
    :param content: Content to hash.
    :param expected_hash: Expected hash of the content.
    :return: The computed hash of the content. Can return None if the operation fails for some reason.
    """

    # TODO: get a better way to compare hashes (without depending on the IPFS daemon)
    try:
        cid_version = 0
        if len(expected_hash) >= 58:
            cid_version = 1

        computed_hash = await add_ipfs_bytes(content, cid_version=cid_version)
        return computed_hash

    except asyncio.TimeoutError:
        LOGGER.warning(f"Timeout while computing IPFS hash for '{expected_hash}'.")
        return None


async def compute_content_hash_sha256(content: bytes, expected_hash: str) -> str:
    return get_sha256(content)


async def verify_content_hash(
    content: bytes, engine: ItemType, expected_hash: str
) -> None:
    """
    Checks that the hash of a content we fetched from the network matches the expected hash.
    :return: True if the hashes match, False otherwise.
    """
    ipfs_enabled = app["config"].ipfs.enabled.value

    if engine == ItemType.IPFS and ipfs_enabled:
        compute_hash = compute_content_hash_ipfs
    elif engine == ItemType.Storage:
        compute_hash = compute_content_hash_sha256
    else:
        raise ValueError(f"Invalid storage engine: '{engine}'.")

    computed_hash = await compute_hash(content, expected_hash)

    if computed_hash is None:
        error_msg = f"Could not compute hash for '{expected_hash}'."
        LOGGER.warning(error_msg)
        raise ContentCurrentlyUnavailable(error_msg)

    if computed_hash != expected_hash:
        error_msg = f"Got a bad hash! Expected '{expected_hash}' but computed '{computed_hash}'."
        LOGGER.warning(error_msg)
        raise InvalidContent(error_msg)


async def get_hash_content(
    content_hash: str,
    engine: ItemType = ItemType.IPFS,
    timeout: int = 2,
    tries: int = 1,
    use_network: bool = True,
    use_ipfs: bool = True,
    store_value: bool = True,
) -> RawContent:
    # TODO: determine which storage engine to use
    ipfs_enabled = app["config"].ipfs.enabled.value

    source = None

    # Try to retrieve the data from the DB, then from the network or IPFS.
    content = await get_value(content_hash)
    if content is not None:
        source = ContentSource.DB

    if content is None and use_network:
        content = await fetch_content_from_network(content_hash, engine, timeout)
        source = ContentSource.P2P

    if content is None:
        if ipfs_enabled and engine == ItemType.IPFS and use_ipfs:
            content = await get_ipfs_content(content_hash, timeout=timeout, tries=tries)
            source = ContentSource.IPFS

    if content is None:
        raise ContentCurrentlyUnavailable(
            f"Could not fetch content for '{content_hash}'."
        )

    LOGGER.info("Got content from %s for '%s'.", source.value, content_hash)  # type: ignore

    # Store content locally if we fetched it from the network
    if store_value and source != ContentSource.DB:
        LOGGER.debug(f"Storing content for '{content_hash}'.")
        await set_value(content_hash, content)

    return RawContent(hash=content_hash, value=content, source=source)


async def get_json(
    content_hash: str, engine=ItemType.IPFS, timeout: int = 2, tries: int = 1
) -> JsonContent:
    content = await get_hash_content(
        content_hash, engine=engine, timeout=timeout, tries=tries
    )

    try:
        json_content = await json_async_loads(content.value)
    except json.JSONDecodeError as e:
        LOGGER.exception("Can't decode JSON")
        raise InvalidContent("Cannot decode JSON") from e

    return JsonContent(
        hash=content.hash,
        value=json_content,
        source=content.source,
        raw_content=content.value,
    )


async def pin_hash(chash: str, timeout: int = 2, tries: int = 1):
    return await ipfs_pin_add(chash, timeout=timeout, tries=tries)


async def add_json(value: Any, engine: ItemType = ItemType.IPFS) -> str:
    # TODO: determine which storage engine to use
    content = await run_in_executor(None, json.dumps, value)
    content = content.encode("utf-8")
    if engine == ItemType.IPFS:
        chash = await add_ipfs_bytes(content)
    elif engine == ItemType.Storage:
        if isinstance(content, str):
            content = content.encode("utf-8")
        chash = sha256(content).hexdigest()
    else:
        raise NotImplementedError("storage engine %s not supported" % engine)

    await set_value(chash, content)
    return chash


async def add_file(
    fileobject: IO, filename: Optional[str] = None, engine: ItemType = ItemType.IPFS
) -> str:

    if engine == ItemType.IPFS:
        output = await ipfs_add_file(fileobject, filename)
        file_hash = output["Hash"]
        fileobject.seek(0)
        file_content = fileobject.read()

    elif engine == ItemType.Storage:
        file_content = fileobject.read()
        file_hash = sha256(file_content).hexdigest()

    else:
        raise ValueError(f"Unsupported item type: {engine}")

    await set_value(file_hash, file_content)
    return file_hash
