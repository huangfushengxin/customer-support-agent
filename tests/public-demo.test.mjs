import test from 'node:test';
import assert from 'node:assert/strict';
import {DemoStore, STORAGE_KEY} from '../docs/demo-store.mjs';
import {faq} from '../docs/faq.mjs';
class Storage {
  data = new Map();
  getItem(key) { return this.data.get(key) ?? null; }
  setItem(key,value) { this.data.set(key,value); }
}
function setup(){const storage=new Storage(), store=new DemoStore(storage,faq), session=store.request('/agent/sessions',{});return {storage,store,url:'/agent/sessions/'+session.thread_id};}
test('FAQ replies cite matching documents and do not create tickets',()=>{
  const {store,url}=setup();const result=store.request(url+'/messages',{message:'激活失败怎么办'});
  assert.equal(result.sources,undefined);assert.equal(result.history[1].sources[0].id,'FAQ-001');
  assert.equal(result.draft,null);assert.equal(store.tickets().length,0);
});
test('followups preserve original facts across new store instances and invalidate old tokens',()=>{
  const {store,storage,url}=setup();const first=store.request(url+'/messages',{message:'激活失败，帮我整理工单'});
  const reopened=new DemoStore(storage,faq);const second=reopened.request(url+'/messages',{message:'补充版本2.0，Windows 11'});
  assert.match(second.draft.description,/激活失败/);assert.match(second.draft.description,/版本2.0/);
  assert.match(second.draft.description,/Windows 11/);assert.equal(second.history.length,4);
  assert.deepEqual(second.draft.missing_info,[]);assert.notEqual(first.draft_token,second.draft_token);
  assert.throws(()=>reopened.request(url+'/confirm',{draft_token:first.draft_token}),/版本已变化/);
  assert.equal(store.tickets().length,0);
});
test('confirmation is explicit, idempotent and closes the saved session',()=>{
  const {store,url}=setup();const draft=store.request(url+'/messages',{message:'无法激活，请整理工单'});
  assert.equal(store.tickets().length,0);
  const first=store.request(url+'/confirm',{draft_token:draft.draft_token});
  const second=store.request(url+'/confirm',{draft_token:draft.draft_token});
  assert.equal(first.saved_ticket_id,second.saved_ticket_id);assert.equal(second.confirmation.already_saved,true);
  assert.equal(store.tickets().length,1);assert.equal(store.tickets()[0].description,draft.draft.description);
  assert.throws(()=>store.request(url+'/messages',{message:'改一下'}),/已保存/);
});
test('different visitors and sessions cannot see each others records',()=>{
  const {store,url}=setup();store.request(url+'/messages',{message:'用户A工单'});
  const second=store.request('/agent/sessions',{});assert.equal(second.history.length,0);
  const other=new DemoStore(new Storage(),faq);assert.equal(other.request('/agent/sessions').sessions.length,0);
  assert.throws(()=>other.request(url),/未找到/);assert.equal(other.tickets().length,0);
});
test('unknown questions do not fabricate FAQ evidence',()=>{
  const {store,url}=setup();const state=store.request(url+'/messages',{message:'明天的天气怎么样'});
  assert.equal(state.history[1].sources.length,0);assert.match(state.history[1].content,/没有找到匹配依据/);
});
test('failed persistence does not silently commit a turn',()=>{
  const {store,storage,url}=setup();const before=storage.getItem(STORAGE_KEY);
  storage.setItem=()=>{throw new Error('quota');};
  assert.throws(()=>store.request(url+'/messages',{message:'生成工单'}),/本次操作未保存/);
  assert.equal(storage.getItem(STORAGE_KEY),before);assert.equal(store.request(url).history.length,0);
});
test('blank, non-string and oversized inputs are rejected without state changes',()=>{
  const {store,url}=setup();for(const message of ['  ',42,'x'.repeat(2001)]){
    assert.throws(()=>store.request(url+'/messages',{message}),/1～2000/);
  }assert.equal(store.request(url).history.length,0);
});
