'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {loadOrderFormReferences} = require('../home-master/web/order-form.js');

test('returns ready lists independently and keeps numeric IDs', async () => {
  const refs = await loadOrderFormReferences(async (_method, path) => path === '/departments'
    ? [{id: 17, name: 'Repair'}]
    : [{id: 29, full_name: 'Test Customer'}], 1);
  assert.equal(refs.departments.state, 'ready');
  assert.equal(refs.departments.items[0].id, 17);
  assert.equal(refs.clients.state, 'ready');
  assert.equal(refs.clients.items[0].id, 29);
});

test('distinguishes an empty directory from a failed API request', async () => {
  const refs = await loadOrderFormReferences(async (_method, path) => {
    if (path === '/departments') return [];
    throw new Error('503 service unavailable');
  }, 1);
  assert.equal(refs.departments.state, 'empty');
  assert.equal(refs.clients.state, 'error');
  assert.match(refs.clients.error, /503/);
});

test('does not request client directory for roles without client selection', async () => {
  const paths = [];
  const refs = await loadOrderFormReferences(async (_method, path) => {
    paths.push(path);
    return [];
  }, 4);
  assert.deepEqual(paths, ['/departments']);
  assert.equal(refs.clients.state, 'not-applicable');
});
