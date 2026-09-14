# ManageBac Bark Notifier (Downstream Consumer)

This is a standalone consumer application for `mb-cli`. It receives ManageBac events (either via HTTP webhook or direct Python SDK) and sends push notifications to iOS devices via Bark with custom course aliases and alarm sounds.

## Requirements
- Python 3.10+
- `requests`
- `mb-cli` (installed via pip or git)

## Usage with mb-cli Daemon
1. Start this receiver:
   ```bash
   python bark_webhook_receiver.py --port 42617 --host 127.0.0.1
   ```
2. Start mb daemon to dispatch events:
   ```bash
   mb daemon run --webhook-url http://127.0.0.1:42617/webhook
   ```
