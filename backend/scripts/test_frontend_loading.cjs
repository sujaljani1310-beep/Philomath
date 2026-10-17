// Execute the actual TypeScript callbacks with synthetic React/Supabase state.
// No frontend dependencies or source files are modified; no network is used.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const {createRequire} = require('node:module');
const root = path.resolve(__dirname, '../..');
const ts = createRequire(path.join(root, 'frontend/package.json'))('typescript');
const text = fs.readFileSync(path.join(root, 'frontend/app/page.tsx'), 'utf8');
const source = ts.createSourceFile('page.tsx', text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const callbacks = {};
function visit(node) {
  if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
    if (['loadIntegrations', 'loadConversations', 'loadConversationMessages'].includes(node.name.text))
      callbacks[node.name.text] = node.initializer.arguments[0].getText(source);
    if (node.name.text === 'handleSendMessage') callbacks.send = node.initializer.getText(source);
    if (node.name.text === 'handleAddAI') callbacks.addAI = node.initializer.getText(source);
  }
  if (ts.isCallExpression(node) && node.expression.getText(source) === 'useEffect') {
    const body = node.arguments[0].getText(source);
    if (body.includes('supabase.auth.getSession')) callbacks.auth = body;
    if (body.includes('conv.historyStatus')) callbacks.history = body;
  }
  ts.forEachChild(node, visit);
}
visit(source);
const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((a,b) => {resolve=a; reject=b;});
  return {promise, resolve, reject};
}
function state() {
  const pending = deferred();
  const observed = {fetches: 0, auth: null};
  const c = {
    session: {access_token: 'synthetic-token', user: {id:'a'}}, conversations: [],
    activeConversationId: 'chat', backendUrl: 'http://synthetic.invalid',
    authGenerationRef:{current:0}, authIdentityRef:{current:'a'},
    historyRequestsRef:{current:new Set()}, inputValue:'draft', isLoading:false,
    pendingConversationRef:{current:null},
    FormData,
    console:{log(){}}, showToast(){},
    authorizedFetchWithRetry: async () => {observed.fetches++; return {ok:true,json:async()=>({messages:[]})};},
    getSupabaseBrowserClient: () => ({auth:{
      getSession:()=>pending.promise,
      onAuthStateChange:fn=>{observed.auth=fn; return {data:{subscription:{unsubscribe(){}}}};},
    }}),
  };
  for (const [setter,field] of Object.entries({setSession:'session', setAuthLoading:'authLoading',
    setConversations:'conversations', setActiveConversationId:'activeConversationId',
    setIntegrations:'integrations',setIntegrationsLoading:'integrationsLoading',
    setSelectedProvider:'selectedProvider',setSelectedFiles:'selectedFiles',setInputValue:'inputValue',
    setAiApiKey:'aiApiKey',setAiName:'aiName',setAddAiOpen:'addAiOpen',setStartupLoading:'startupLoading',
    setIsLoading:'isLoading',setAddingAi:'addingAi',setSettingsOpen:'settingsOpen'}))
    c[setter]=value=>{c[field]=typeof value==='function' ? value(c[field]) : value;};
  vm.createContext(c);
  const functions={};
  for (const [name,callback] of Object.entries(callbacks)) {
    const js=ts.transpileModule(`(${callback})`,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
    functions[name]=vm.runInContext(js,c);
  }
  c.loadConversationMessages=functions.loadConversationMessages;
  return {c,functions,pending,observed};
}

test('empty successful history loads once across rerenders', async()=>{
  const {c,functions,observed}=state(); c.conversations=[{id:'chat',messages:[]}];
  functions.history(); await flush();
  for(let i=0;i<5;i++){functions.history(); await flush();}
  assert.equal(observed.fetches,1); assert.equal(c.conversations[0].historyStatus,'loaded');
});
test('a local unsaved new chat performs no history request',()=>{
  const {c,functions,observed}=state(); c.conversations=[{id:'chat',messages:[],historyStatus:'loaded'}];
  functions.history(); assert.equal(observed.fetches,0);
});
test('inflight history is deduplicated',async()=>{
  const {c,functions,observed}=state(), reply=deferred(); c.conversations=[{id:'chat',messages:[]}];
  c.authorizedFetchWithRetry=()=>{observed.fetches++;return reply.promise;};
  const a=functions.loadConversationMessages('chat'), b=functions.loadConversationMessages('chat');
  assert.equal(observed.fetches,1); reply.resolve({ok:true,json:async()=>({messages:[]})}); await a;await b;
  assert.equal(c.historyRequestsRef.current.size,0);
});
test('a failed history load stops automatic loops and permits explicit retry',async()=>{
  const {c,functions,observed}=state(); c.conversations=[{id:'chat',messages:[]}];
  c.authorizedFetchWithRetry=async()=>{observed.fetches++;return {ok:false};};
  functions.history(); await flush(); functions.history(); await flush();
  assert.equal(observed.fetches,1); assert.equal(c.conversations[0].historyStatus,'failed');
  await functions.loadConversationMessages('chat'); assert.equal(observed.fetches,2);
});
test('history rejection releases its inflight marker',async()=>{
  const {c,functions}=state();c.conversations=[{id:'chat',messages:[]}];
  c.authorizedFetchWithRetry=async()=>{throw new Error('synthetic');};
  await functions.loadConversationMessages('chat');assert.equal(c.historyRequestsRef.current.size,0);
  assert.equal(c.conversations[0].historyStatus,'failed');
});
test('sending waits for pending history and retains draft',async()=>{
  const {c,functions,observed}=state();c.activeConversation={historyStatus:'loading'};
  await functions.send();assert.equal(observed.fetches,0);assert.equal(c.inputValue,'draft');
});
test('a failed-history retry waits for pending generation',async()=>{
  const {c,functions,observed}=state();c.isLoading=true;
  c.conversations=[{id:'chat',messages:[{content:'optimistic draft'}],historyStatus:'failed'}];
  await functions.loadConversationMessages('chat');
  assert.equal(observed.fetches,0);assert.equal(c.conversations[0].messages[0].content,'optimistic draft');
});
test('switching chats cannot bypass the pending-generation history guard',async()=>{
  const {c,functions,observed}=state();
  // A has a pending reply; the user selected B before retrying A's failed history.
  c.isLoading=true;c.activeConversationId='other-chat';
  c.conversations=[{id:'chat',messages:[{content:'optimistic draft'}],historyStatus:'failed'},
                   {id:'other-chat',messages:[],historyStatus:'loaded'}];
  await functions.loadConversationMessages('chat');
  assert.equal(observed.fetches,0);
  assert.equal(c.historyRequestsRef.current.size,0);
  assert.equal(c.conversations[0].historyStatus,'failed');
  assert.equal(c.conversations[0].messages[0].content,'optimistic draft');
  c.isLoading=false;
  await functions.loadConversationMessages('chat');
  assert.equal(observed.fetches,1);assert.equal(c.conversations[0].historyStatus,'loaded');
});
test('a token-refresh listing preserves an existing pending turn and its reply',async()=>{
  const {c,functions}=state(), reply=deferred();
  c.selectedMode='basic';c.selectedFiles=[];
  c.activeConversation={id:'chat',title:'Saved',messages:[{content:'earlier turn'}],historyStatus:'loaded'};
  c.conversations=[c.activeConversation];c.authorizedFetch=()=>reply.promise;
  c.authorizedFetchWithRetry=async()=>({ok:true,json:async()=>({conversations:[{id:'chat',title:'Saved'}]})});
  const work=functions.send();
  await functions.loadConversations();
  assert.equal(c.conversations[0].messages.length,2);
  assert.equal(c.conversations[0].messages[1].content,'draft');
  reply.resolve({ok:true,json:async()=>({answer:'pending answer'})});await work;
  assert.equal(c.conversations[0].messages.length,3);
  assert.equal(c.conversations[0].messages[2].content,'pending answer');
  assert.equal(c.pendingConversationRef.current,null);
});
test('a token-refresh listing cannot discard a new chat before its first save',async()=>{
  const {c,functions}=state(), reply=deferred();
  c.selectedMode='basic';c.selectedFiles=[];c.activeConversationId=null;c.activeConversation=null;
  c.createConversationId=()=> 'new-pending-chat';c.authorizedFetch=()=>reply.promise;
  c.authorizedFetchWithRetry=async()=>({ok:true,json:async()=>({conversations:[]})});
  const work=functions.send();
  await functions.loadConversations();
  assert.equal(c.conversations.length,1);assert.equal(c.conversations[0].id,'new-pending-chat');
  reply.resolve({ok:true,json:async()=>({answer:'first answer'})});await work;
  assert.equal(c.conversations[0].messages.length,2);
  assert.equal(c.conversations[0].messages[1].content,'first answer');
  assert.equal(c.pendingConversationRef.current,null);
});
test('a refreshed listing preserves loaded empty history without fetching again',async()=>{
  const {c,functions,observed}=state();c.conversations=[{id:'chat',messages:[],historyStatus:'loaded'}];
  c.authorizedFetchWithRetry=async()=>{observed.fetches++;return {ok:true,json:async()=>({conversations:[{id:'chat',title:'Updated title'}]})};};
  await functions.loadConversations();functions.history();await flush();
  assert.equal(observed.fetches,1);assert.equal(c.conversations[0].historyStatus,'loaded');
  assert.equal(c.conversations[0].title,'Updated title');
});
test('a queued listing update retains the pending chat after generation clears its ref',async()=>{
  const {c,functions}=state(), reply=deferred();
  c.selectedMode='basic';c.selectedFiles=[];c.activeConversationId=null;c.activeConversation=null;
  c.createConversationId=()=> 'new-pending-chat';c.authorizedFetch=()=>reply.promise;
  c.authorizedFetchWithRetry=async()=>({ok:true,json:async()=>({conversations:[]})});
  const work=functions.send(), queued=[];
  // React may defer both state updaters until after the async callback's finally.
  c.setConversations=update=>queued.push(update);
  await functions.loadConversations();
  reply.resolve({ok:true,json:async()=>({answer:'queued answer'})});await work;
  assert.equal(c.pendingConversationRef.current,null);
  for(const update of queued) c.conversations=update(c.conversations);
  assert.equal(c.conversations.length,1);assert.equal(c.conversations[0].messages.length,2);
  assert.equal(c.conversations[0].messages[1].content,'queued answer');
});
test('late history from the prior account is ignored',async()=>{
  const {c,functions}=state(), reply=deferred();c.conversations=[{id:'chat',messages:[]}];
  c.authorizedFetchWithRetry=()=>reply.promise;
  const work=functions.loadConversationMessages('chat');c.authGenerationRef.current++;
  c.conversations=[{id:'chat',messages:[{content:'new account'}]}];
  reply.resolve({ok:true,json:async()=>({messages:[{content:'old account',role:'user'}]})});await work;
  assert.equal(c.conversations[0].messages[0].content,'new account');
});
for(const loader of ['loadConversations','loadIntegrations']) test(`late ${loader} cannot restore prior-account data`,async()=>{
  const {c,functions}=state(), reply=deferred();c.authorizedFetchWithRetry=()=>reply.promise;
  const work=functions[loader]();c.authGenerationRef.current++;
  c.conversations=[];c.integrations=[];c.integrationsLoading=true;
  reply.resolve({ok:true,json:async()=>({conversations:[{id:'private-old'}],integrations:[{provider:'old'}]})});await work;
  assert.equal(c.conversations.length,0);assert.equal(c.integrations.length,0);assert.equal(c.integrationsLoading,true);
});
test('rejected getSession clears loading without throwing',async()=>{
  const {c,functions,pending}=state();functions.auth();pending.reject(new Error('synthetic'));
  await flush();assert.equal(c.authLoading,false);assert.equal(c.session,null);
});
test('a current auth event wins over a late initial session snapshot',async()=>{
  const {c,functions,pending,observed}=state();functions.auth();
  observed.auth('SIGNED_IN',{user:{id:'new'},access_token:'new-token'});
  pending.resolve({data:{session:null}});await flush();assert.equal(c.session.user.id,'new');
});
test('switching accounts clears scoped drafts and keys',()=>{
  const {c,functions,observed}=state();functions.auth();c.authIdentityRef.current='old';
  c.conversations=[{id:'old'}];c.aiApiKey='synthetic-old-key';c.inputValue='old draft';c.pendingConversationRef.current='old';
  observed.auth('SIGNED_IN',{user:{id:'new'},access_token:'new-token'});
  assert.equal(c.conversations.length,0);assert.equal(c.aiApiKey,'');assert.equal(c.inputValue,'');
  assert.equal(c.pendingConversationRef.current,null);
});
test('unmounted auth callbacks cannot update state',async()=>{
  const {c,functions,pending}=state();const cleanup=functions.auth();cleanup();
  pending.resolve({data:{session:{user:{id:'old'}}}});await flush();assert.notEqual(c.session.user.id,'old');
});
for(const loader of ['loadConversations','loadIntegrations','loadConversationMessages']) test(`stale ${loader} closure cannot start work for another account`,async()=>{
  const {c,functions,observed}=state();c.authIdentityRef.current='new-account';
  await functions[loader]('chat');assert.equal(observed.fetches,0);
});
test('a late BYOK save cannot reload or clear the new account',async()=>{
  const {c,functions}=state(), reply=deferred();let reloads=0;
  c.aiName='openrouter';c.aiApiKey='synthetic-old-key';c.authorizedFetch=()=>reply.promise;
  c.loadIntegrations=async()=>{reloads++;};
  const work=functions.addAI();c.authGenerationRef.current++;c.authIdentityRef.current='new-account';
  c.aiApiKey='synthetic-new-draft';c.addingAi=false;
  reply.resolve({ok:true,json:async()=>({integration:{display_name:'old-account'}})});await work;
  assert.equal(reloads,0);assert.equal(c.aiApiKey,'synthetic-new-draft');assert.equal(c.addingAi,false);
});
test('a late upload cannot create a conversation or start generation after account switch',async()=>{
  const {c,functions}=state(), reply=deferred();let calls=0;
  c.selectedMode='manual';c.selectedProvider='openrouter';c.selectedFiles=[new Blob(['synthetic old file'])];
  c.activeConversationId=null;c.activeConversation=null;
  c.authorizedFetch=()=>{calls++;return reply.promise;};
  const work=functions.send();c.authGenerationRef.current++;c.authIdentityRef.current='new-account';
  c.conversations=[];c.isLoading=false;c.inputValue='new draft';
  reply.resolve({ok:true,json:async()=>({success:true,combined_text:'old file',count:1})});await work;
  assert.equal(calls,1);assert.equal(c.conversations.length,0);assert.equal(c.inputValue,'new draft');
});
test('a late model response cannot append messages to the next account',async()=>{
  const {c,functions}=state(), reply=deferred();
  c.selectedMode='basic';c.selectedFiles=[];c.activeConversation={id:'chat',messages:[],historyStatus:'loaded'};
  c.conversations=[c.activeConversation];c.authorizedFetch=()=>reply.promise;
  const work=functions.send();c.authGenerationRef.current++;c.authIdentityRef.current='new-account';
  c.conversations=[];c.isLoading=false;c.pendingConversationRef.current='new-account-chat';
  reply.resolve({ok:true,json:async()=>({answer:'old private reply'})});await work;
  assert.equal(c.conversations.length,0);assert.equal(c.isLoading,false);
  assert.equal(c.pendingConversationRef.current,'new-account-chat');
});
