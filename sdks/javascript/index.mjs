/** The gateway verifies HMAC and consumes approvals; no signing secret reaches this SDK. */
export class ActionBlocked extends Error {}
export class Gateway {
  constructor(baseURL, virtualKey, {fetchImpl = fetch} = {}) {
    this.baseURL = baseURL.replace(/\/$/, '');
    this.virtualKey = virtualKey;
    this.fetch = fetchImpl;
  }
  async request(path, body) {
    const response = await this.fetch(this.baseURL + path, {
      method: body === undefined ? 'GET' : 'POST',
      signal: AbortSignal.timeout(30000),
      headers: {'content-type': 'application/json', authorization: `Bearer ${this.virtualKey}`},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
    if (!response.ok) throw new ActionBlocked(`Gateway rejected action (${response.status})`);
    return response.json();
  }
  async check_action(actionName, action, summary, {waitTimeout = 0, pollInterval = 1000} = {}) {
    let result = await this.request('/v1/actions/check', {action_name: actionName, action, summary});
    const deadline = Date.now() + waitTimeout;
    while (result.decision === 'pending' && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, Math.min(pollInterval, deadline - Date.now())));
      result = await this.request(`/v1/actions/${result.approval_id}`);
    }
    return result;
  }
  async verify_token(token, actionName, action) {
    const result = await this.request('/v1/actions/consume', {token, action_name: actionName, action});
    if (!result.consumed) throw new ActionBlocked('Approval was not consumed');
    return true;
  }
  async execute(actionName, action, summary, fn, options = {waitTimeout: 900000}) {
    const result = await this.check_action(actionName, action, summary, options);
    if (result.decision !== 'allow' || !result.token) throw new ActionBlocked(`Action is ${result.decision}`);
    await this.verify_token(result.token, actionName, action);
    return fn();
  }
}
