from logging import getLogger
import asyncio
from aleph.chains.common import incoming, get_chaindata_messages
from aleph.model.pending import PendingMessage, PendingTX
from pymongo import DeleteOne, InsertOne

LOGGER = getLogger("JOBS")

RETRY_LOCK = asyncio.Lock()


async def handle_pending_message(pending, seen_ids, actions_list):
    result = await incoming(
        pending['message'],
        chain_name=pending['source'].get('chain_name'),
        tx_hash=pending['source'].get('tx_hash'),
        height=pending['source'].get('height'),
        seen_ids=seen_ids[pending['source'].get('chain_name')],
        check_message=pending['source'].get('check_message', True),
        retrying=True)

    if result is True:  # message handled (valid or not, we don't care)
        # Is it valid to add to a list passed this way? to be checked.
        actions_list.append(DeleteOne({'_id': pending['_id']}))


async def join_pending_message_tasks(tasks, actions_list):
    try:
        await asyncio.gather(*tasks)
    except Exception:
        LOGGER.exception("error in incoming task")
    tasks.clear()

    if len(actions_list):
        await PendingMessage.collection.bulk_write(actions_list)
        actions_list.clear()


async def retry_messages_job():
    """ Each few minutes, try to handle message that were added to the
    pending queue (Unavailable messages)."""

    seen_ids = {
        'NULS': [],
        'ETH': [],
        'BNB': []
    }
    actions = []
    tasks = []
    i = 0
    async for pending in PendingMessage.collection.find().sort([('time', 1)]).limit(1000):
        i += 1
        tasks.append(handle_pending_message(pending, seen_ids, actions))

        if (i > 200):
            await join_pending_message_tasks(tasks, actions)
            i = 0

    await join_pending_message_tasks(tasks, actions)


async def retry_messages_task():
    while True:
        try:
            await retry_messages_job()
        except Exception:
            LOGGER.exception("Error in pending messages retry job")

        await asyncio.sleep(1)
        

async def handle_pending_tx(pending, actions_list):
    messages = await get_chaindata_messages(pending['content'], pending['context'])
    if isinstance(messages, list):
        message_actions = list()
        for message in messages:
            message['time'] = pending['context']['time']
            
            # we add it to the message queue... bad idea? should we process it asap?
            message_actions.append(InsertOne({
                'message': message,
                'source': dict(
                    chain_name=pending['context']['chain_name'],
                    tx_hash=pending['context']['tx_hash'],
                    height=pending['context']['height'],
                    check_message=True  # should we store this?
                )
            }))
            
        if message_actions:
            await PendingMessage.collection.bulk_write(message_actions)
            
    if messages is not None:
        # bogus or handled, we remove it.
        actions_list.append(DeleteOne({'_id': pending['_id']}))
        

async def join_pending_txs_tasks(tasks, actions_list):
    try:
        await asyncio.gather(*tasks)
    except Exception:
        LOGGER.exception("error in incoming txs task")
    tasks.clear()

    if len(actions_list):
        await PendingTX.collection.bulk_write(actions_list)
        actions_list.clear()


async def handle_txs_job():
    """ Each few minutes, try to handle message that were added to the
    pending queue (Unavailable messages)."""

    actions = []
    tasks = []
    i = 0
    async for pending in PendingTX.collection.find().sort([('time', 1)]).limit(1000):
        i += 1
        tasks.append(handle_pending_tx(pending, actions))

        if (i > 100):
            await join_pending_txs_tasks(tasks, actions)
            i = 0

    await join_pending_txs_tasks(tasks, actions)


async def handle_txs_task():
    while True:
        try:
            LOGGER.info("handling TXs")
            await handle_txs_job()
        except Exception:
            LOGGER.exception("Error in pending txs job")

        await asyncio.sleep(1)

def start_jobs():
    LOGGER.info("starting jobs")
    loop = asyncio.get_event_loop()
    loop.create_task(retry_messages_task())
    loop.create_task(handle_txs_task())
