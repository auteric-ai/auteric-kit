// Deliberately custom response shape and routes; no framework route declarations.
// Backend contract: /merchandise?term=... returns { results: [...] }.
// /merchandise/ITEM returns { record: ... }. amount is decimal USD, not cents.
import http from 'node:http';
const records = [
  { code: 'A1', label: 'Canvas boots', amount: '45.00', money: 'USD', stock: 'in_stock' },
  { code: 'B2', label: 'Summer sandals', amount: '24.50', money: 'USD', stock: 'out_of_stock' },
];
http.createServer((req,res)=>{
  const url=new URL(req.url,'http://127.0.0.1');
  res.setHeader('content-type','application/json');
  if(req.method!=='GET'){res.writeHead(405);res.end('{}');return;}
  if(url.pathname==='/merchandise'){
    const term=(url.searchParams.get('term')||'').toLowerCase();
    res.end(JSON.stringify({results:records.filter(row=>row.label.toLowerCase().includes(term))}));return;
  }
  const record=records.find(row=>url.pathname==='/merchandise/'+row.code);
  if(record){res.end(JSON.stringify({record}));return;}
  res.writeHead(404);res.end('{}');
}).listen(5187,'127.0.0.1',()=>console.log('Acceptance merchant ready on 5187'));
