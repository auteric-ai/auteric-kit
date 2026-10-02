import {readFileSync,existsSync,mkdirSync,writeFileSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {homedir} from 'node:os';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';

export function bundledHarness(options={}) {
  const archive=fileURLToPath(new URL('../../runtime/connect-harness.zip',import.meta.url));
  const manifest=JSON.parse(readFileSync(new URL('../../runtime/connect-harness.json',import.meta.url),'utf8'));
  const hash=createHash('sha256').update(readFileSync(archive)).digest('hex');
  if(hash!==manifest.sha256)throw Error('runtime_api_incompatible: bundled Gateway harness digest differs');
  const source=join(resolve(process.env.AUTERIC_CACHE_DIRECTORY || join(homedir(),'.cache/auteric')),'connect-harness',hash);
  const python=options.python || 'python3';
  const executable=join(source,'venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
  const ready=join(source,'.ready');
  if(!existsSync(ready)) {
    mkdirSync(source,{recursive:true});
    try {
      execFileSync(python,['-c',`import sys,zipfile,pathlib\nassert sys.version_info >= (3,11), 'Python 3.11+ is required'\nroot=pathlib.Path(sys.argv[2]).resolve()\nwith zipfile.ZipFile(sys.argv[1]) as archive:\n for name in archive.namelist():\n  assert (root/name).resolve().is_relative_to(root), 'unsafe harness archive'\n archive.extractall(root)`,archive,source],{stdio:'inherit'});
      execFileSync(python,['-m','venv',join(source,'venv')],{stdio:'inherit'});
      execFileSync(executable,['-m','pip','install','--disable-pip-version-check','-r',join(source,'requirements.txt')],{stdio:'inherit'});
      writeFileSync(ready,hash+'\n');
    } catch(error) {rmSync(ready,{force:true});throw Error('local_prerequisite_missing: cannot prepare the versioned Gateway harness: '+error.message);}
  }
  return {source,python:executable,sourceCommit:manifest.control_image};
}
