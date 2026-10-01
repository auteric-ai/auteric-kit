import Ajv from 'ajv/dist/2020.js';
import {readFileSync} from 'node:fs';

// Pinned to the qualified Control image's public registration model. This
// checks the complete request, including native and generated Sidecar fields.
const schema=JSON.parse(readFileSync(new URL('../../schemas/installation-registration.schema.json',import.meta.url),'utf8'));
const validate=new Ajv({strict:false,allErrors:true}).compile(schema);
export function validateRegistration(body) {
  if(!validate(body))throw Error('runtime_api_incompatible: installation request violates the qualified Control contract: '+
    validate.errors.map(error=>`${error.instancePath || '/'} ${error.message}`).join('; '));
  return body;
}
