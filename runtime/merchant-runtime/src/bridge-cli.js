import { readFileSync } from 'node:fs';
import { applicationAdapters } from './application-bridge.js';
import { createApplicationBridge } from './bridge-server.js';
const options={origin:process.env.AUTERIC_MERCHANT_ORIGIN,installationId:process.env.AUTERIC_INSTALLATION_ID,statePath:process.env.AUTERIC_BRIDGE_STATE,bindingDigest:process.env.AUTERIC_BINDING_DIGEST,databaseUrl:process.env.AUTERIC_STATE_DATABASE_URL,applicationToken:process.env.AUTERIC_APPLICATION_TOKEN,privateHosts:[new URL(process.env.AUTERIC_MERCHANT_ORIGIN).hostname]};
const translator=process.env.AUTERIC_ADAPTER_CONNECTION
  ? await (await import('./module-adapter.js')).moduleAdapters(process.env.AUTERIC_ADAPTER_CONNECTION,options)
  : applicationAdapters(JSON.parse(readFileSync(process.env.AUTERIC_MAPPING,'utf8')),options);
const bridge=createApplicationBridge({adapters:translator.adapters,token:process.env.AUTERIC_BRIDGE_TOKEN,port:3101});
await bridge.listen();
const stop=async()=>{await bridge.close();await translator.close();};
process.once('SIGTERM',()=>void stop());process.once('SIGINT',()=>void stop());
