import { GatewayTransport, defineGatewayTool, registerGatewayTools } from '/lab-sdk/index.js';

const id = { type: 'string', minLength: 1, maxLength: 200 };
const quantity = { type: 'integer', minimum: 1, maximum: 10000 };
const object = (properties, required = []) => ({ type: 'object', properties, required, additionalProperties: false });
const item = object({ product_id: id, variant_id: id, quantity }, ['product_id', 'quantity']);
const schemas = {
  search_products: object({ query: { type: 'string', maxLength: 500 }, limit: { type: 'integer', minimum: 1, maximum: 100 } }),
  get_product: object({ product_id: id }, ['product_id']),
  create_cart: object({ currency: { type: 'string', enum: ['USD'] }, items: { type: 'array', items: item, maxItems: 100 } }),
  get_cart: object({ cart_id: id }, ['cart_id']),
  add_to_cart: object({ cart_id: id, product_id: id, variant_id: id, quantity }, ['cart_id', 'product_id', 'quantity']),
  update_cart_item: object({ cart_id: id, product_id: id, quantity }, ['cart_id', 'product_id', 'quantity']),
  remove_from_cart: object({ cart_id: id, product_id: id }, ['cart_id', 'product_id']),
  create_checkout: object({ cart_id: id }, ['cart_id']),
  get_checkout: object({ checkout_id: id }, ['checkout_id']),
};
const reads = new Set(['search_products', 'get_product', 'get_cart', 'get_checkout']);
const label = document.getElementById('webmcp-status');
let registration;

async function start() {
  window.autericLabBootstrap ??= fetch('/api/auteric/bootstrap', {
    credentials: 'same-origin', redirect: 'error',
  }).then(response => {
    if (!response.ok) throw new Error('Local session initialization failed');
    return response.json();
  });
  await window.autericLabBootstrap;
  const gateway = new GatewayTransport({
    fetch: async (url, options) => {
      const response = await fetch(url, options);
      // Inform the local operator UI of native requests. This does not approve,
      // replay or alter the BFF response returned to the SDK.
      if (response.ok && response.headers.get('content-type')?.includes('application/json')) {
        const outcome = await response.clone().json();
        const request = JSON.parse(options.body);
        if (outcome.request_id === request.request_id) {
          window.dispatchEvent(new CustomEvent('auteric-gateway-outcome', {
            detail: { ...outcome, operation: request.operation, input: request.input },
          }));
        }
      }
      return response;
    },
  });
  const tools = Object.entries(schemas).map(([operation, inputSchema]) => defineGatewayTool({
    gateway, operation, name: operation, stableKey: `auteric.lab.${operation}.v1`,
    description: `${operation.replaceAll('_', ' ')} through the local Auteric policy gateway. Checkout creates a handoff only.`,
    inputSchema,
    risk: { sensitivity: reads.has(operation) ? 'public' : 'sensitive',
      sideEffect: reads.has(operation) ? 'read' : 'write', requiresApproval: !reads.has(operation) },
  }));
  registration = await registerGatewayTools(tools);
  if (label) {
    label.dataset.status = registration.status;
    label.dataset.toolCount = String(registration.supported ? registration.names.length : 0);
    label.textContent = registration.supported
      ? `${registration.names.length} WebMCP tools registered — Auteric enforces every action.`
      : 'Native WebMCP is unavailable in this browser. Store controls use the same Auteric gateway.';
  }
}

window.addEventListener('pagehide', () => { void registration?.unregister(); }, { once: true });
start().catch(error => {
  if (label) { label.dataset.status = 'failed'; label.textContent = `WebMCP initialization failed: ${error.message}`; }
});
