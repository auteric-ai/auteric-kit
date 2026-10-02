import {existsSync, readFileSync, readdirSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {writeArtifacts,writeDeploymentPlan} from '../connect/artifacts.js';
import {shared} from '../connect/shared.js';
import {readJSON} from '../workflow.js';
import {runtimeInfrastructure} from './infrastructure.js';

const json=value=>JSON.stringify(value,null,2)+'\n';

// Deployment references are evidence supplied by the current coding model, not
// a new commerce mapping language. Never manufacture cloud identities or data.
export async function prepareDeployment(root,{domain,environment,deployment}={}) {
  const files=[];
  for(const name of ['Dockerfile','compose.yaml','compose.yml','docker-compose.yml','deploy/task-definition.json'])
    if(existsSync(join(root,name)))files.push(name);
  const workflows=join(root,'.github/workflows');
  if(existsSync(workflows))for(const name of readdirSync(workflows))if(/\.ya?ml$/.test(name))files.push('.github/workflows/'+name);
  const context=readJSON(join(root,'auteric/.state/deployment-context.json')) || {};
  if(context.infrastructure)await writeArtifacts(root,{'auteric/infrastructure.json':json(runtimeInfrastructure(context.infrastructure))});
  const source=deployment || (existsSync(join(root,'auteric/deployment.json'))?'auteric/deployment.json':null);
  let config=source ? readJSON(resolve(root,source)) : context.deployment;
  if(config) {
    const {validateDocument}=await shared('mapping');
    config=validateDocument('deployment',config);
  }
  const tasks=files.filter(name=>name.endsWith('task-definition.json'));
  const taskPath=config?.task_definition || context.task_definition || tasks[0];
  const task=taskPath ? readJSON(resolve(root,taskPath)) : null;
  const platform=context.platform || (task?'ecs-fargate':files.some(name=>/compose/.test(name))?'compose':null);
  const blockers=[];
  if(!platform && !config)blockers.push({code:'deployment_platform_unknown',owner:'current_coding_model',reason:'Trace the actual hosting/deployment workflow; do not infer production hosting from frontend tooling.'});
  if(!config)blockers.push({code:'deployment_references_missing',owner:'current_coding_model',reason:'Inspect existing cloud/Compose resources with authorized read-only access. Reuse proven network, storage and secret references, or prepare owned infrastructure with an explicit cost decision.'});
  const merchant=config?.merchant_service || context.merchant_service || (task?.containerDefinitions?.length===1?task.containerDefinitions[0].name:null);
  const container=task?.containerDefinitions?.find(c=>c.name===merchant);
  const database=container?.environment?.find(e=>e.name==='DATABASE_PATH')?.value;
  if(config?.platform==='ecs-fargate' && (!config.task_definition || !existsSync(resolve(root,config.task_definition))))
    blockers.push({code:'task_source_missing',owner:'current_coding_model',reason:'Identify the actual ECS source task before owner enrollment; rendering needs an existing verified task.'});
  if(database && (!container.mountPoints?.some(m=>database.startsWith(m.containerPath+'/')) || database.startsWith('/tmp/')))
    blockers.push({code:'merchant_storage_not_durable',owner:'merchant_decision',reason:'The source task stores business data outside a persistent mount. Inspect the deployed task before changing it; prepare a data migration only after the owner chooses it.'});
  if(task && !context.live_task_verified && !source)
    blockers.push({code:'live_task_not_verified',owner:'current_coding_model',reason:'Compare repository task with the deployed task using read-only ECS inspection. A historical task is not proof of current storage.'});
  if(!environment)blockers.push({code:'environment_unconfirmed',owner:'current_coding_model',reason:'Determine the requested deployment environment from the user and verified deployment. Do not silently enroll production.'});
  if(config && context.deployment && !source && blockers.length===0)await writeArtifacts(root,{'auteric/deployment.json':json(config)});
  const plan={schema:'auteric-deployment-plan/v1',status:blockers.length?'deployment_preparation_required':'enrollment_pending',
    domain:domain||null,environment:environment||null,platform:config?.platform||platform,source_files:files,
    deployment:blockers.length===0 ? (source || (config?'auteric/deployment.json':null)) : null,
    blockers,steps:[
      {stage:'inspect',owner:'current_coding_model',action:'Trace deployment and compare live references; preserve merchant command, auth, transaction boundary and business database.'},
      {stage:'infrastructure',owner:'current_coding_model',action:'Reuse existing resources. For missing owned runtime resources prepare infrastructure template and request only required access/cost decisions.'},
      {stage:'enrollment',owner:'auteric',action:'Continue the same Connect request through Control owner login and enrollment after deployment prerequisites pass.'},
      {stage:'render',owner:'auteric',action:'Render runtime-connection plus ECS task/Compose and ingress intent from exact enrollment; patch the actual merchant deployment workflow.'},
      {stage:'deploy',owner:'merchant_approval',action:'Execute the reviewed workflow only when deployment is authorized; run public Connection Test afterward.'}],
    public_verified:false,production_ready:false,
    continuation:{owner:'current_coding_model',stop_for_user:false,
      instructions:'Do not end at local verification. Read deployment-preparation.md, resolve model-owned blockers in this request, prepare reversible owned deployment files, and resume Connect internally. Ask only for unavailable access, ambiguous target, business-data migration or resource cost approval. Never deploy without authorization.'}};
  await writeDeploymentPlan(root,json(plan));
  return plan;
}
