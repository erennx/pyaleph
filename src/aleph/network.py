import asyncio
import json
import logging
from typing import Coroutine, List, Dict
from urllib.parse import unquote

from aleph.chains.register import VERIFIER_REGISTER
from aleph.services.ipfs.pubsub import incoming_channel as incoming_ipfs_channel
from aleph.types import ItemType
from aleph.utils import get_sha256

LOGGER = logging.getLogger("NETWORK")

MAX_INLINE_SIZE = 200000  # 200kb max inline content size.

INCOMING_MESSAGE_AUTHORIZED_FIELDS = [
    "item_hash",
    "item_content",
    "item_type",
    "chain",
    "channel",
    "sender",
    "type",
    "time",
    "signature",
]

HOST = None


async def incoming_check(ipfs_pubsub_message):
    """Verifies an incoming message is sane, protecting from spam in the
    meantime.

    TODO: actually implement this, no check done here yet. IMPORTANT.
    """

    try:
        message_data = ipfs_pubsub_message.get("data", b"").decode("utf-8")
        message = json.loads(unquote(message_data))
        LOGGER.debug("New message! %r" % message)
        message = await check_message(message, from_network=True)
        return message
    except json.JSONDecodeError:
        LOGGER.exception(
            "Received non-json message %r" % ipfs_pubsub_message.get("data", "")
        )


async def check_message(message: Dict, from_chain=False, from_network=False, trusted=False):
    """This function should check the incoming message and verify any
    extraneous or dangerous information for the rest of the process.
    It also checks the data hash if it's not done by an external provider (ipfs)
    and the data length.
    Example of dangerous data: fake confirmations, fake tx_hash, bad times...

    If a item_content is there, set the item_type to inline, else to ipfs (default).

    TODO: Implement it fully! Dangerous!
    """
    if not isinstance(message["item_hash"], str):
        LOGGER.warning("Unknown hash %s" % message["item_hash"])
        return None

    if not isinstance(message["chain"], str):
        LOGGER.warning("Unknown chain %s" % message["chain"])
        return None

    if message.get("channel", None) is not None:
        if not isinstance(message.get("channel", None), str):
            LOGGER.warning("Unknown channel %s" % message["channel"])
            return None

    if not isinstance(message["sender"], str):
        LOGGER.warning("Unknown sender %s" % message["sender"])
        return None

    if not isinstance(message["signature"], str):
        LOGGER.warning("Unknown signature %s" % message["signature"])
        return None

    if message.get("item_content", None) is not None:
        if len(message["item_content"]) > MAX_INLINE_SIZE:
            LOGGER.warning("Message too long")
            return None
        await asyncio.sleep(0)

        if message.get("hash_type", "sha256") == "sha256":  # leave the door open.
            if not trusted:
                loop = asyncio.get_event_loop()
                item_hash = get_sha256(message["item_content"])
                # item_hash = await loop.run_in_executor(None, get_sha256, message['item_content'])
                # item_hash = sha256(message['item_content'].encode('utf-8')).hexdigest()

                if message["item_hash"] != item_hash:
                    LOGGER.warning("Bad hash")
                    return None
        else:
            LOGGER.warning("Unknown hash type %s" % message["hash_type"])
            return None

        message["item_type"] = ItemType.Inline

    else:
        try:
            message["item_type"] = ItemType.from_hash(message["item_hash"])
        except ValueError:
            pass

    if trusted:
        # only in the case of a message programmatically built here
        # from legacy native chain signing for example (signing offloaded)
        return message
    else:
        message = {
            k: v for k, v in message.items() if k in INCOMING_MESSAGE_AUTHORIZED_FIELDS
        }
        await asyncio.sleep(0)
        chain = message.get("chain", None)
        signer = VERIFIER_REGISTER.get(chain, None)
        if signer is None:
            LOGGER.warning("Unknown chain for validation %r" % chain)
            return None
        try:
            if await signer(message):
                return message
        except ValueError:
            LOGGER.warning("Signature validation error")
            return None


def listener_tasks(config) -> List[Coroutine]:
    from aleph.services.p2p import incoming_channel as incoming_p2p_channel

    # for now (1st milestone), we only listen on a single global topic...
    tasks: List[Coroutine] = [
        incoming_p2p_channel(config, config.aleph.queue_topic.value)
    ]
    if config.ipfs.enabled.value:
        tasks.append(incoming_ipfs_channel(config, config.aleph.queue_topic.value))
    return tasks
