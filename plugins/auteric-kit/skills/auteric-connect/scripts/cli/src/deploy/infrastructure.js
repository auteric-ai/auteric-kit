// Secrets remain durable in the platform; runtime state lives in Auteric.
export function runtimeInfrastructure({task_role,execution_role,create_database=false}) {
  if(create_database)throw Error('Gateway-backed runtime requires no PostgreSQL/RDS; remove create_database');
  if(![task_role,execution_role].every(role=>/^[A-Za-z0-9+=,.@_-]{1,64}$/.test(role||'')))throw Error('verified task and execution role names required');
  const ref=name=>({Ref:name});
  return {AWSTemplateFormatVersion:'2010-09-09',Description:'Auteric installation secrets only; no database, disk, network or merchant service changes',Resources:{
    RuntimeEnrollment:{Type:'AWS::SecretsManager::Secret',Properties:{SecretString:'{}',Description:'Auteric installation identity'}},
    ApplicationToken:{Type:'AWS::SecretsManager::Secret',Properties:{GenerateSecretString:{PasswordLength:64,ExcludePunctuation:true},Description:'Private merchant application authentication'}},
    RuntimeAccess:{Type:'AWS::IAM::Policy',Properties:{PolicyName:{'Fn::Sub':'${AWS::StackName}-identity'},Roles:[task_role],PolicyDocument:{Version:'2012-10-17',Statement:[{Effect:'Allow',Action:['secretsmanager:GetSecretValue','secretsmanager:PutSecretValue'],Resource:[ref('RuntimeEnrollment')]}]}}},
    ExecutionAccess:{Type:'AWS::IAM::Policy',Properties:{PolicyName:{'Fn::Sub':'${AWS::StackName}-injection'},Roles:[execution_role],PolicyDocument:{Version:'2012-10-17',Statement:[{Effect:'Allow',Action:['secretsmanager:GetSecretValue'],Resource:[ref('ApplicationToken')]}]}}}
  },Outputs:{EnrollmentSecret:{Value:ref('RuntimeEnrollment')},ApplicationSecret:{Value:ref('ApplicationToken')}},Metadata:{Auteric:{merchant_database_modified:false,public_service_modified:false,runtime_storage:'gateway/v1'}}};
}
