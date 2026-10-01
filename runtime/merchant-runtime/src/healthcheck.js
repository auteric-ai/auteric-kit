const response=await fetch(`http://127.0.0.1:${process.env.AUTERIC_RUNTIME_PORT||7080}/health/ready`,{signal:AbortSignal.timeout(3000)});
process.exitCode=response.ok?0:1;
