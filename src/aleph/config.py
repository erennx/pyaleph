# settings.py
import pathlib
import yaml

def get_defaults():
    return {
        'aleph': {
            'queue_topic': 'ALEPH-QUEUE',
            'host': '127.0.0.1',
            'port': 8080
        },
        'nulsexplorer': {
            'host': '127.0.0.1',
            'port': 8080
        },
        'nuls': {
          'chain_id': 8964,
          'packing_node': False,
          'private_key': None
        },
        'mongodb': {
          'uri': 'mongodb://127.0.0.1:27006',
          'database': 'aleph'
        },
        'mail': {
            'email_sender': 'aleph@localhost.localdomain',
            'smtp_url': 'smtp://localhost'
        },
        'ipfs': {
            'host': '127.0.0.1',
            'port': 5001
        }
    }
