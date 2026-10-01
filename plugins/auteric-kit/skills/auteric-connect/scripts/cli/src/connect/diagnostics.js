import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/** Source hints are gaps, never capability or verification evidence. */
export function expressHTTPGaps(root,inventory) {
  if(!inventory.drivers?.includes('express'))return [];
  const candidates=(inventory.candidates || []).find(item=>item.operation==='add_to_cart')?.evidence?.entrypoints || [];
  const eligible=candidates.filter(r=>r.method==='POST'&&!/admin|checkout|payment/i.test(r.path));
  if(eligible.length!==1)return eligible.length>1?['multiple cart write routes require --mapping PATH']:[];
  const route=eligible[0];
  let source;try{source=readFileSync(resolve(root,route.file),'utf8');}catch{return ['cart write route source unavailable'];}
  const lines=source.split('\n'),start=route.line-1;
  let end=start+1;
  while(end<lines.length&&!/\b(?:registerRoute|app\.(?:get|post|put|patch|delete)|router\.(?:get|post|put|patch|delete))\s*\(/.test(lines[end]))end++;
  const handler=lines.slice(start,end).join('\n');
  if(!/expected_?revision|if-match/i.test(handler))return [`${route.method} ${route.path} (${route.file}:${route.line}) has no explicit expected_revision forwarding; atomic revision support must be tested at this HTTP boundary`];
  return [];
}
