import { shared } from '../connect/shared.js';

/** Pure task renderer. No AWS mutations, ALB registration or new resource creation. */
export async function renderECS(task, connection, deployment, release) {
  const {validateConnection}=await shared('mapping');const {qualifyRelease}=await shared('release');
  validateConnection(connection,{privateHosts:[deployment.merchant_service]});qualifyRelease(release,deployment);
  if(deployment.platform!=='ecs-fargate'||!deployment.secret_ref.startsWith('arn:aws:secretsmanager:'))throw Error('ECS requires an existing Secrets Manager reference');
  if(deployment.state_ref.profile!=='postgresql/v1')throw Error('storage_profile_unsupported: ECS requires qualified PostgreSQL; ephemeral SQLite and EFS WAL are unsupported');
  if(task.networkMode!=='awsvpc'||!task.requiresCompatibilities?.includes('FARGATE')||!task.executionRoleArn)throw Error('existing Fargate task and execution role required');
  const isModule=connection.mapping.schema==='auteric-module-binding/v1';
  if(isModule && (!deployment.adapter_mount || !deployment.application_secret_ref?.startsWith('arn:aws:secretsmanager:')))
    throw Error('module ECS requires adapter volume and shared private application secret reference');
  const out=structuredClone(task);

  if(!out.containerDefinitions?.some(c=>c.name===deployment.merchant_service))throw Error('selected merchant container missing');
  if(out.containerDefinitions.some(c=>c.name==='auteric-runtime'))throw Error('existing runtime must be reconciled explicitly');
  if(!out.volumes?.some(v=>v.name===deployment.discovery_mount))throw Error('existing discovery volume reference required');
  if(isModule) {
    const merchant=out.containerDefinitions.find(c=>c.name===deployment.merchant_service);
    if(!out.volumes?.some(v=>v.name===deployment.adapter_mount))out.volumes.push({name:deployment.adapter_mount});
    if(!merchant.healthCheck)throw Error('module ECS requires the merchant healthcheck');
    merchant.mountPoints=[...(merchant.mountPoints||[]),{sourceVolume:deployment.adapter_mount,containerPath:'/auteric-integration',readOnly:false}];
    if((merchant.environment||[]).some(e=>e.name==='AUTERIC_RUNTIME_ORIGIN') || (merchant.secrets||[]).some(e=>e.name==='AUTERIC_APPLICATION_TOKEN'))throw Error('existing Auteric deployment must be reconciled explicitly');
    merchant.environment=[...(merchant.environment||[]),{name:'AUTERIC_RUNTIME_ORIGIN',value:'http://127.0.0.1:'+deployment.runtime_port}];
    merchant.secrets=[...(merchant.secrets||[]),{name:'AUTERIC_APPLICATION_TOKEN',valueFrom:deployment.application_secret_ref}];
    // Dockerfile exports adapter bytes through VOLUME /auteric-integration.
    // ECS populates the shared volume; the original merchant startup stays intact.
  }
  out.containerDefinitions.push({name:'auteric-runtime',image:deployment.runtime_image_digest,essential:true,cpu:256,memory:512,
    dependsOn:[{containerName:deployment.merchant_service,condition:'HEALTHY'}],
    portMappings:[{containerPort:deployment.runtime_port,protocol:'tcp'}],
    environment:[{name:'AUTERIC_CONNECTION_JSON',value:JSON.stringify(connection)},...(isModule?[{name:'AUTERIC_ADAPTER_CONNECTION',value:'/integration/connection.json'}]:[]),{name:'AUTERIC_CONTROL_ORIGIN',value:connection.control_origin},{name:'AUTERIC_STORE_ID',value:connection.store_id},
      {name:'AUTERIC_INSTALLATION_ID',value:connection.installation_id},{name:'AUTERIC_MAPPING_DIGEST',value:connection.mapping_digest},
      {name:'AUTERIC_RUNTIME_RELEASE',value:connection.runtime_release},{name:'AUTERIC_MERCHANT_ORIGIN',value:connection.backend.origin},
      {name:'AUTERIC_SERVICE_SECRET',value:deployment.secret_ref},{name:'AUTERIC_STATE',value:'/tmp/auteric'},
      {name:'AUTERIC_DISCOVERY',value:'/discovery/ucp'},{name:'AUTERIC_RUNTIME_PORT',value:String(deployment.runtime_port)}],
    secrets:[{name:'AUTERIC_STATE_DATABASE_URL',valueFrom:deployment.state_ref.reference},...(isModule?[{name:'AUTERIC_APPLICATION_TOKEN',valueFrom:deployment.application_secret_ref}]:[])],
    mountPoints:[{sourceVolume:deployment.discovery_mount,containerPath:'/discovery',readOnly:false},...(isModule?[{sourceVolume:deployment.adapter_mount,containerPath:'/integration',readOnly:true}]:[])],
    user:'10001:10001',
    ...(out.containerDefinitions.find(c=>c.name===deployment.merchant_service)?.logConfiguration?{logConfiguration:structuredClone(out.containerDefinitions.find(c=>c.name===deployment.merchant_service).logConfiguration)}:{}),
    healthCheck:{command:['CMD','node','/app/src/healthcheck.js'],interval:15,timeout:5,retries:3,startPeriod:30}});
  // Existing tiny tasks must have capacity for the additional generic container.
  const cpu=Number(out.cpu),memory=Number(out.memory);
  if(cpu===256){out.cpu='512';out.memory=String(Math.max(memory,1024));}
  if(Number(out.memory)<out.containerDefinitions.reduce((sum,c)=>sum+(c.memory||c.memoryReservation||0),0))
    throw Error('deployment_prerequisite_missing: task memory must fit merchant and runtime; model must prepare valid Fargate sizing');
  // Bridge stays on loopback inside this container and never appears in ALB intent.
  return {taskDefinition:out,ingress:{reference:deployment.ingress_ref,container:'auteric-runtime',port:deployment.runtime_port,paths:['/api/auteric/v1/*','/api/auteric/agent/v1/*','/.well-known/ucp']}};
}
