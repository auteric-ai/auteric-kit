// Two HTTP fixtures share business behavior, but expose independent route/field vocabularies.
// The service owns transactions, stock, sessions, revisions and idempotency.
import { createServer } from 'node:http';
import { DatabaseSync } from 'node:sqlite';
import { createHash, randomUUID } from 'node:crypto';
import { REGISTRY_DIGEST } from '../src/contracts.generated.js';

export function fixtureMapping(style='a') {
  const b=style==='b';
  const names=b?{id:'key',title:'label',variants:'offers',price:'cost',currency:'denomination',available:'sale',items:'entries',revision:'version',subtotal:'sum',total:'due',discount:'saving'}:
    {id:'id',title:'name',variants:'variants',price:'priceCents',currency:'currency',available:'availability',items:'items',revision:'revision',subtotal:'subtotalCents',total:'totalCents',discount:'discountCents'};
  const routes=b?{session:'/visitors',search:'/wares',product:'/wares/{key}',create:'/baskets',cart:'/baskets/{key}',add:'/baskets/{key}/entries'}:
    {session:'/api/session/guest',search:'/api/products',product:'/api/products/{id}',create:'/api/carts',cart:'/api/carts/{id}',add:'/api/carts/{id}/items'};
  const copy=select=>({select,transform:'copy'});
  const id=kind=>({select:'/'+names.id,transform:'id',kind});
  const money=field=>({select:'/'+field,transform:'money',currency:'/'+names.currency,unit:b?'decimal':'minor'});
  const identity=select=>({select,transform:'id',kind:select==='/cart_id'?'cart':select==='/variant_id'?'variant':'product'});
  const op=(operation,route,fields={},path={})=>({operation,target:{method:['create_cart','add_to_cart'].includes(operation)?'POST':'GET',path:route},
    input:{path,query:operation==='search_products'?fields:{},body:operation==='search_products'?{}:fields},
    output:{profile:operation==='search_products'?'search':operation==='get_product'?'product':'cart',select:''},
    auth:['search_products','get_product'].includes(operation)?'none':'session',
    ...(['create_cart','add_to_cart'].includes(operation)?{idempotency:{header:'idempotency-key'}}:{}),
    ...(operation==='add_to_cart'?{revision:{location:'body',field:b?'ifVersion':'expectedRevision'}}:{})});
  return {schema:'auteric-bridge-mapping/v1',registry_digest:REGISTRY_DIGEST,identity_profile:'installation-buyer-cookie/v1',session:{method:'POST',path:routes.session,cookie_name:'visitor'},
    profiles:{
      search:{results:{select:b?'/matches':'/items',transform:'array',profile:'product'},page:{select:'/page',transform:'object',profile:'pagination'}},
      pagination:{has_more:copy('/has_more'),next_cursor:{select:'/next_cursor',transform:'copy',optional:true}},
      product:{product_id:id('product'),title:copy('/'+names.title),variants:{select:'/'+names.variants,transform:'array',profile:'variant'}},
      variant:{variant_id:id('variant'),price:money(names.price),available:{select:'/'+names.available,transform:'enum',values:b?{yes:true,no:false}:{in_stock:true,out_of_stock:false}}},
      cart:{cart_id:id('cart'),resource_revision:copy('/'+names.revision),status:copy('/status'),currency:copy('/'+names.currency),line_items:{select:'/'+names.items,transform:'array',profile:'line'},discounts:{select:'/'+names.discount,transform:b?'copy':'zero_discounts'},subtotal:money(names.subtotal),discount_total:money(b?'discountAmount':names.discount),total:money(names.total)},
      line:{line_id:id('line'),product_id:{select:'/product',transform:'id',kind:'product'},variant_id:{select:'/variant',transform:'id',kind:'variant'},title:copy('/'+names.title),quantity:copy('/quantity'),unit_price:money(names.price),total_price:money(b?'rowTotal':'lineTotalCents')},
    },
    operations:[op('search_products',routes.search,{[b?'term':'query']:copy('/q'),limit:copy('/limit'),page:{select:'/cursor',transform:'cursor_page'}}),
      op('get_product',routes.product,{}, {[b?'key':'id']:identity('/product_id')}),
      op('create_cart',routes.create,{[names.currency]:copy('/currency')}),
      op('get_cart',routes.cart,{}, {[b?'key':'id']:identity('/cart_id')}),
      op('add_to_cart',routes.add,{[b?'ware':'productId']:identity('/product_id'),[b?'offer':'variantId']:identity('/variant_id'),quantity:copy('/quantity')},{[b?'key':'id']:identity('/cart_id')})],
  };
}
export async function startMerchant(path,style='a') {
  const b=style==='b', db=new DatabaseSync(path), mapping=fixtureMapping(style);
  db.exec('CREATE TABLE IF NOT EXISTS carts(id TEXT PRIMARY KEY,owner TEXT,body TEXT); CREATE TABLE IF NOT EXISTS replays(owner TEXT,key TEXT,hash TEXT,body TEXT,PRIMARY KEY(owner,key));');
  const products=[{id:'p-one',variant:'v-one',title:'Blue shoe',price:1250,stock:20},{id:'p-two',variant:'v-two',title:'Sold shoe',price:800,stock:0}];
  const amount=n=>b?(n/100).toFixed(2):n;
  const product=p=>b?{key:p.id,label:p.title,offers:[{key:p.variant,cost:amount(p.price),denomination:'USD',sale:p.stock?'yes':'no'}]}:
    {id:p.id,name:p.title,variants:[{id:p.variant,priceCents:p.price,currency:'USD',availability:p.stock?'in_stock':'out_of_stock'}]};
  const cart=c=> {
    const items=c.items.map(i=>{const p=products.find(p=>p.id===i.product);return b?
      {key:i.id,product:p.id,variant:p.variant,label:p.title,quantity:i.quantity,cost:amount(p.price),rowTotal:amount(p.price*i.quantity),denomination:c.currency}:
      {id:i.id,product:p.id,variant:p.variant,name:p.title,quantity:i.quantity,priceCents:p.price,lineTotalCents:p.price*i.quantity,currency:c.currency};});
    const sum=c.items.reduce((sum,i)=>sum+products.find(p=>p.id===i.product).price*i.quantity,0);
    return b?{key:c.id,version:c.revision,status:'active',denomination:c.currency,entries:items,saving:[],discountAmount:'0.00',sum:amount(sum),due:amount(sum)}:
      {id:c.id,revision:c.revision,status:'active',currency:c.currency,items,discountCents:0,subtotalCents:sum,totalCents:sum};
  };
  const server=createServer((req,res)=>void(async()=>{
    const send=(status,value,headers={})=>{res.writeHead(status,{'content-type':'application/json',...headers});res.end(JSON.stringify(value));};
    let text='';for await(const chunk of req)text+=chunk;
    const body=text?JSON.parse(text):{}, url=new URL(req.url,'http://localhost'), owner=/(?:^|;\s*)visitor=([^;]+)/.exec(req.headers.cookie||'')?.[1];
    if(req.method==='POST' && url.pathname===mapping.session.path)return send(200,{kind:'guest'},{'set-cookie':`visitor=${randomUUID()}; Path=/; HttpOnly; Max-Age=3600`});
    const root=b?'/wares':'/api/products', basket=b?'/baskets':'/api/carts';
    if(req.method==='GET' && url.pathname===root) {
      const rows=products.filter(p=>p.title.toLowerCase().includes((url.searchParams.get(b?'term':'query')||'').toLowerCase()));
      const limit=Number(url.searchParams.get('limit')||20),page=Number(url.searchParams.get('page')||1),more=page*limit<rows.length;
      return send(200,{[b?'matches':'items']:rows.slice((page-1)*limit,page*limit).map(product),page:{has_more:more,...(more?{next_cursor:String(page+1)}:{})}});
    }
    if(req.method==='GET' && url.pathname.startsWith(root+'/')) {
      const p=products.find(p=>p.id===decodeURIComponent(url.pathname.slice(root.length+1)));
      return send(p?200:404,p?product(p):{code:'PRODUCT_NOT_FOUND'});
    }
    if(!owner)return send(401,{code:'SESSION_REQUIRED'});
    const parts=url.pathname.split('/'), id=parts[b?2:3];
    if(req.method==='GET' && url.pathname.startsWith(basket+'/')) {
      const row=db.prepare('SELECT body FROM carts WHERE id=? AND owner=?').get(id,owner);
      return send(row?200:404,row?cart(JSON.parse(row.body)):{code:'CART_NOT_FOUND'});
    }
    if(req.method!=='POST'||!url.pathname.startsWith(basket))return send(404,{code:'NOT_FOUND'});
    const key=req.headers['idempotency-key'];if(!key)return send(400,{code:'IDEMPOTENCY_REQUIRED'});
    const hash=createHash('sha256').update(url.pathname+'\n'+text).digest('hex');
    db.exec('BEGIN IMMEDIATE');
    try {
      const replay=db.prepare('SELECT hash,body FROM replays WHERE owner=? AND key=?').get(owner,key);
      if(replay){db.exec('ROLLBACK');return send(replay.hash===hash?200:409,replay.hash===hash?JSON.parse(replay.body):{code:'IDEMPOTENCY_CONFLICT'});}
      let c;
      if(url.pathname===basket) {
        const currency=body[b?'denomination':'currency'];if(currency!=='USD'){db.exec('ROLLBACK');return send(400,{code:'CURRENCY_REQUIRED'});}
        c={id:randomUUID(),owner,currency,revision:0,items:[]};
      } else {
        const row=db.prepare('SELECT body FROM carts WHERE id=? AND owner=?').get(id,owner);
        if(!row){db.exec('ROLLBACK');return send(404,{code:'CART_NOT_FOUND'});}
        c=JSON.parse(row.body);
        const expected=body[b?'ifVersion':'expectedRevision'];
        if(expected!==undefined && expected!==c.revision){db.exec('ROLLBACK');return send(409,{code:'REVISION_CONFLICT'});}
        const p=products.find(p=>p.id===body[b?'ware':'productId'] && p.variant===body[b?'offer':'variantId']);
        if(!p){db.exec('ROLLBACK');return send(404,{code:'PRODUCT_NOT_FOUND'});}
        const quantity=body.quantity, existing=c.items.find(i=>i.product===p.id);
        if(!Number.isInteger(quantity)||quantity<1||quantity+(existing?.quantity||0)>p.stock){db.exec('ROLLBACK');return send(409,{code:'OUT_OF_STOCK'});}
        if(existing)existing.quantity+=quantity;else c.items.push({id:randomUUID(),product:p.id,quantity});c.revision++;
      }
      const output=cart(c);
      db.prepare('INSERT INTO carts VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body').run(c.id,owner,JSON.stringify(c));
      db.prepare('INSERT INTO replays VALUES(?,?,?,?)').run(owner,key,hash,JSON.stringify(output));db.exec('COMMIT');return send(200,output);
    }catch(e){db.exec('ROLLBACK');throw e;}
  })().catch(()=>{res.writeHead(500);res.end();}));
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  return {mapping,origin:`http://127.0.0.1:${server.address().port}`,close:async()=>{await new Promise(resolve=>server.close(resolve));db.close();}};
}
