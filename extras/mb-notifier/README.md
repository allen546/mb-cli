# ManageBac Bark Notifier (Downstream Consumer)

This is a standalone consumer application for `tahuti`. It receives ManageBac events (either via HTTP webhook or direct Python SDK) and sends push notifications to iOS devices via Bark with custom course aliases and alarm sounds.

## Requirements
- Python 3.10+
- `requests`
- `tahuti` (installed via pip or git)

## Usage with tahuti Daemon
1. Start this receiver:
   ```bash
   python bark_webhook_receiver.py --port 42617 --host 127.0.0.1
   ```
2. Start tahuti daemon to dispatch events:
   ```bash
   tahuti daemon run --webhook-url http://127.0.0.1:42617/webhook
   ```
