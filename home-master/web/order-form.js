(function (root) {
  'use strict';

  function normalizeReferenceResult(result, label) {
    if (!Array.isArray(result)) {
      return {state: 'error', items: [], error: label + ' API returned a non-array response'};
    }
    return {state: result.length ? 'ready' : 'empty', items: result, error: null};
  }

  async function loadOrderFormReferences(api, role) {
    const wantsClients = [1, 2, 7].includes(Number(role));
    const requests = await Promise.allSettled([
      api('GET', '/departments'),
      wantsClients ? api('GET', '/users?role=4') : Promise.resolve([])
    ]);
    const map = (entry, label) => entry.status === 'fulfilled'
      ? normalizeReferenceResult(entry.value, label)
      : {state: 'error', items: [], error: entry.reason && entry.reason.message ? entry.reason.message : String(entry.reason || label + ' API error')};
    return {
      departments: map(requests[0], 'Departments'),
      clients: wantsClients ? map(requests[1], 'Clients') : {state: 'not-applicable', items: [], error: null}
    };
  }

  root.loadOrderFormReferences = loadOrderFormReferences;
  if (typeof module !== 'undefined' && module.exports) module.exports = {loadOrderFormReferences};
})(globalThis);
