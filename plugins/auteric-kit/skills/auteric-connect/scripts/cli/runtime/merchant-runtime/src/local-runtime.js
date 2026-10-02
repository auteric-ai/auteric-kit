import { remoteState } from './remote-store.js';
import { spawn } from 'node:child_process';
import { moduleAdapters } from './module-adapter.js';
import { createApplicationBridge } from './bridge-server.js';
import { stopChild } from './manager.js';
import { createServer, connect } from 'node:net';
import { readFileSync } from 'node:fs';

// Local acceptance uses the same release image and dispatcher for every adapter.
// No enrollment, release qualification, discovery or synthetic activation occurs here.
export async function startLocalRuntime() {
  const config=JSON.parse(readFileSync(process.env.AUTERIC_SIDECAR_CONFIG,'utf8'));
  const profiles=Object.values(config.integration.profiles), bindingDigest=profiles[0]?.mapping_fingerprint;
  if(!bindingDigest || profiles.some(p=>p.mapping_fingerprint!==bindingDigest))throw Error('inconsistent registered adapter binding');
  // Docker lab only: preserve the SDK's dev-loopback URL while forwarding bytes
  // to the actual local Gateway. Credentials, signing and policy remain unchanged.
  const gatewayPort = Number(process.env.AUTERIC_LOCAL_GATEWAY_PORT);
  const gateway = gatewayPort ? createServer(socket => {
    const upstream=connect(gatewayPort,'host.docker.internal');
    upstream.on('error',()=>socket.destroy()); socket.on('error',()=>upstream.destroy());
    socket.pipe(upstream).pipe(socket);
  }) : null;
  if(gateway) await new Promise((ok,bad)=>gateway.once('error',bad).listen(gatewayPort,'127.0.0.1',ok));
  const translator = await moduleAdapters(process.env.AUTERIC_CONNECTION, {
    origin: process.env.AUTERIC_MERCHANT_ORIGIN, installationId: process.env.AUTERIC_INSTALLATION_ID,
    applicationToken: process.env.AUTERIC_APPLICATION_TOKEN, statePath: process.env.AUTERIC_BRIDGE_STATE,
    bindingDigest, store:remoteState({url:process.env.AUTERIC_RUNTIME_STATE_URL,token:process.env.AUTERIC_SIDECAR_GATEWAY_TOKEN}),
    privateHosts: [new URL(process.env.AUTERIC_MERCHANT_ORIGIN).hostname],
  });
  const bridge = createApplicationBridge({ adapters: translator.adapters, token: process.env.AUTERIC_BRIDGE_TOKEN, port:3101 });
  await bridge.listen();
  const sidecar = spawn('python', ['-m','auteric_merchant.remote_app','--config',process.env.AUTERIC_SIDECAR_CONFIG,
    '--host','0.0.0.0','--port','7070'], {env:process.env,stdio:'inherit'});
  let closing;
  const close = () => closing ||= (async () => { await stopChild(sidecar,3000); await bridge.close(); await translator.close(); gateway?.close(); })();
  sidecar.once('exit', code => { void close().finally(() => { process.exitCode = code || 0; }); });
  process.once('SIGTERM', () => void close()); process.once('SIGINT', () => void close());
  return { close };
}
