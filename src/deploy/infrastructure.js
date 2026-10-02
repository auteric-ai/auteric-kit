// Owned runtime infrastructure only. Merchant database and public service are
// deliberately absent: applying this template cannot migrate business data.
export function runtimeInfrastructure({vpc_id,subnet_ids,merchant_security_group,task_role,execution_role,database_secret_ref,create_database=false,database_instance_class='db.t4g.micro'}) {
  if(!/^vpc-(?:[a-f0-9]{8}|[a-f0-9]{17})$/.test(vpc_id||'') || !/^sg-(?:[a-f0-9]{8}|[a-f0-9]{17})$/.test(merchant_security_group||''))throw Error('verified VPC and merchant security group required');
  if(!Array.isArray(subnet_ids)||subnet_ids.length<2||subnet_ids.some(s=>!/^subnet-(?:[a-f0-9]{8}|[a-f0-9]{17})$/.test(s)))throw Error('verified database subnets in at least two availability zones required');
  if(![task_role,execution_role].every(role=>/^[A-Za-z0-9+=,.@_-]{1,64}$/.test(role||'')))throw Error('verified task and execution role names required');
  if(!create_database&&!/^arn:aws:secretsmanager:[a-z0-9-]+:\d{12}:secret:/.test(database_secret_ref||''))throw Error('existing runtime database URL secret or explicit database creation decision required');
  if(!/^db\.[a-z0-9]+\.[a-z0-9]+$/.test(database_instance_class))throw Error('invalid reviewed database instance class');
  const ref=name=>({Ref:name}),att=(name,attribute)=>({'Fn::GetAtt':[name,attribute]});
  const resources={
    RuntimeEnrollment:{Type:'AWS::SecretsManager::Secret',Properties:{Description:'Auteric installation credential; populated by owner enrollment',SecretString:'{}'}},
    ApplicationToken:{Type:'AWS::SecretsManager::Secret',Properties:{Description:'Private merchant/Auteric application transport',GenerateSecretString:{PasswordLength:64,ExcludePunctuation:true}}}
  };
  let database=database_secret_ref;
  if(create_database) {
    Object.assign(resources,{
      DatabasePassword:{Type:'AWS::SecretsManager::Secret',DeletionPolicy:'Retain',UpdateReplacePolicy:'Retain',Properties:{GenerateSecretString:{PasswordLength:64,ExcludePunctuation:true}}},
      DatabaseNetwork:{Type:'AWS::EC2::SecurityGroup',Properties:{GroupDescription:'Auteric runtime PostgreSQL only',VpcId:vpc_id,SecurityGroupIngress:[{IpProtocol:'tcp',FromPort:5432,ToPort:5432,SourceSecurityGroupId:merchant_security_group}]}},
      DatabaseSubnets:{Type:'AWS::RDS::DBSubnetGroup',Properties:{DBSubnetGroupDescription:'Auteric runtime private database',SubnetIds:subnet_ids}},
      RuntimeDatabase:{Type:'AWS::RDS::DBInstance',DeletionPolicy:'Snapshot',UpdateReplacePolicy:'Snapshot',Properties:{Engine:'postgres',DBInstanceClass:database_instance_class,AllocatedStorage:'20',StorageEncrypted:true,PubliclyAccessible:false,BackupRetentionPeriod:7,DeletionProtection:true,DBName:'auteric',MasterUsername:'auteric',MasterUserPassword:{'Fn::Join':['',['{{resolve:secretsmanager:',ref('DatabasePassword'),':SecretString}}']]},DBSubnetGroupName:ref('DatabaseSubnets'),VPCSecurityGroups:[att('DatabaseNetwork','GroupId')]}},
      DatabaseURL:{Type:'AWS::SecretsManager::Secret',DeletionPolicy:'Retain',UpdateReplacePolicy:'Retain',Properties:{Description:'Auteric runtime PostgreSQL URL',SecretString:{'Fn::Join':['',['postgresql://auteric:{{resolve:secretsmanager:',ref('DatabasePassword'),':SecretString}}@',att('RuntimeDatabase','Endpoint.Address'),':',att('RuntimeDatabase','Endpoint.Port'),'/auteric']]}}}
    });database=ref('DatabaseURL');
  }
  resources.RuntimeAccess={Type:'AWS::IAM::Policy',Properties:{PolicyName:{'Fn::Sub':'${AWS::StackName}-identity'},Roles:[task_role],PolicyDocument:{Version:'2012-10-17',Statement:[{Effect:'Allow',Action:['secretsmanager:GetSecretValue','secretsmanager:PutSecretValue'],Resource:[ref('RuntimeEnrollment')]}]}}};
  resources.ExecutionAccess={Type:'AWS::IAM::Policy',Properties:{PolicyName:{'Fn::Sub':'${AWS::StackName}-injection'},Roles:[execution_role],PolicyDocument:{Version:'2012-10-17',Statement:[{Effect:'Allow',Action:['secretsmanager:GetSecretValue'],Resource:[ref('ApplicationToken'),database]}]}}};
  return {AWSTemplateFormatVersion:'2010-09-09',Description:'Auteric owned runtime resources; no merchant service deployment or business data migration',Resources:resources,
    Outputs:{EnrollmentSecret:{Value:ref('RuntimeEnrollment')},ApplicationSecret:{Value:ref('ApplicationToken')},RuntimeDatabaseSecret:{Value:database}},
    Metadata:{Auteric:{requires_cost_approval:create_database,merchant_database_modified:false,public_service_modified:false}}};
}
