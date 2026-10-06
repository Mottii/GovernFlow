# Action wrappers

Install the Python wrapper with `uv pip install ./sdks/python`, or import `sdks/javascript/index.mjs` in a Node 22+ application. Each wrapper needs a gateway URL and a workflow virtual key.

```python
from governed_automation import Gateway

gateway = Gateway("http://localhost:8000", virtual_key)
await gateway.execute("send_email", {"recipient": recipient, "body": body},
                      "Send customer email", send_email_function)
await gateway.close()
```

```javascript
import {Gateway} from './sdks/javascript/index.mjs';
const gateway = new Gateway('http://localhost:8000', virtualKey);
await gateway.execute('send_email', {recipient, body}, 'Send customer email', sendEmailFunction);
```

`execute` waits for a decision, sends the opaque token and exact action to the gateway, and calls the function only after consumption succeeds. `check_action` and `verify_token` are also available separately. `verify_token` consumes the approval: a second execution attempt is rejected. Recheck an action after a failed execution if another attempt is needed.

Clients never receive the decision signing key. The gateway verifies the HMAC, workflow, action hash, expiry, approval status, and single use in a transaction. Tokens last at most 15 minutes after approval. Configure action names and risk in workflow policy before use; strict unknown-action denial is the default.
