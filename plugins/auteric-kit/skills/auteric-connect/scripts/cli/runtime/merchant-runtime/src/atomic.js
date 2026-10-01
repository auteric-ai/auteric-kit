import { openSync, writeFileSync, fsyncSync, closeSync, renameSync, mkdirSync, lstatSync, unlinkSync } from 'node:fs';
import { dirname, resolve, parse } from 'node:path';
import { randomBytes } from 'node:crypto';

export function safeTarget(path) {
  const target=resolve(path); let cursor=parse(target).root;
  for (const segment of target.slice(cursor.length).split('/')) {
    cursor=resolve(cursor,segment);
    try {if(lstatSync(cursor).isSymbolicLink()) throw Error('refusing symlink artifact target');} catch(e) {if(e.code !== 'ENOENT') throw e;}
  }
  return target;
}
export function atomicWrite(path, content, mode=0o600) {
  const target=safeTarget(path); mkdirSync(dirname(target),{recursive:true,mode:0o700});
  const temp=target+'.'+randomBytes(8).toString('hex')+'.tmp';
  let fd;
  try {fd=openSync(temp,'wx',mode);writeFileSync(fd,content);fsyncSync(fd);closeSync(fd);fd=undefined;renameSync(temp,target);}
  finally {if(fd!==undefined)closeSync(fd);try{unlinkSync(temp);}catch(e){if(e.code!=='ENOENT')throw e;}}
}
