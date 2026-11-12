// Public demo only: deterministic rules and browser-local data, no model or server calls.
export const STORAGE_KEY = 'customer-support-agent:demo:v1';
const clone = value => JSON.parse(JSON.stringify(value));
const id = () => crypto.randomUUID().replaceAll('-', '');
export class DemoStore {
  constructor(storage, faq) { this.storage = storage; this.faq = faq; }
  read() {
    try {
      const raw = this.storage.getItem(STORAGE_KEY);
      const state = raw ? JSON.parse(raw) : {version:1, sessions:{}, tickets:{}, nextId:1};
      if(state.version !== 1 || !state.sessions || !state.tickets || !Number.isInteger(state.nextId)) throw new Error();
      return state;
    } catch { throw new Error('无法读取浏览器演示数据，请允许此站点使用本地存储后重试。'); }
  }
  save(state) {
    try { this.storage.setItem(STORAGE_KEY, JSON.stringify(state)); }
    catch { throw new Error('浏览器存储不可用或空间不足，本次操作未保存。请换用普通浏览窗口。'); }
  }
  request(path, body) {
    const state = this.read();
    if(path === '/agent/sessions') {
      if(body === undefined) return {sessions:Object.values(state.sessions).reverse().map(s => ({
        thread_id:s.thread_id, turns:s.history.length/2, has_draft:!!s.draft,
        ticket_id:s.saved_ticket_id, incomplete:false
      }))};
      if(Object.keys(state.sessions).length >= 30) throw new Error('演示最多保留 30 个会话，请使用已有会话体验。');
      const session = {thread_id:id(), history:[], draft:null, draft_token:null, saved_ticket_id:null, incomplete:false};
      state.sessions[session.thread_id] = session; this.save(state); return clone(session);
    }
    const match = path.match(/^\/agent\/sessions\/([0-9a-f]{32})(?:\/(messages|confirm))?$/);
    if(!match || !Object.hasOwn(state.sessions, match[1])) throw new Error('未找到此浏览器中的演示会话。');
    const session = state.sessions[match[1]];
    if(!match[2]) return clone(session);
    if(match[2] === 'confirm') {
      if(!session.draft || body?.draft_token !== session.draft_token) throw new Error('草稿版本已变化，请刷新并核对最新草稿。');
      const alreadySaved = session.saved_ticket_id !== null;
      if(!alreadySaved) {
        const ticketId = state.nextId++;
        state.tickets[ticketId] = {id:ticketId, ...clone(session.draft), status:'待处理'};
        session.saved_ticket_id = ticketId; this.save(state);
      }
      return {...clone(session), confirmation:{ticket_id:session.saved_ticket_id, already_saved:alreadySaved}};
    }
    if(session.saved_ticket_id) throw new Error('此演示工单已保存，请新建会话处理其他问题。');
    if(session.history.length >= 40) throw new Error('本次演示已达 20 轮，请新建会话继续。');
    if(typeof body?.message !== 'string') throw new Error('请输入 1～2000 字的问题。');
    const message = body.message.trim();
    if(!message.length || message.length > 2000) throw new Error('请输入 1～2000 字的问题。');
    const allInput = [...session.history.filter(m=>m.role==='user').map(m=>m.content), message];
    const context = allInput.join('\n');
    const matched = this.faq.filter(doc => doc.keywords.some(word => message.includes(word)));
    const sources = matched.map(doc=>({id:doc.id, title:doc.title, source:doc.source}));
    let reply = matched.length ? matched.map(doc=>`${doc.content} [${doc.id}]`).join('\n\n') :
      '演示 FAQ 中没有找到匹配依据。可以尝试“激活失败”“忘记密码”“找不到订单”，或说“帮我整理工单”。';
    if(session.draft || /工单|草稿|人工支持|人工处理/.test(message)) {
      const category = /激活|授权|开通/.test(context) ? '激活问题' : /密码|账号|登录/.test(context) ? '账号问题' : /订单|购买/.test(context) ? '订单问题' : '其他问题';
      const missing = [];
      if(!/版本\s*[:：为是]?\s*[vV]?\d/.test(context)) missing.push('软件版本');
      if(!/Windows|Win\s*\d|macOS|Android|Linux|iOS|安卓|鸿蒙/i.test(context)) missing.push('操作系统');
      session.draft = {title:`${category}反馈（模拟整理）`, category,
        description:allInput.map((text,i)=>`用户第${i+1}条：${text}`).join('\n'), missing_info:missing};
      session.draft_token = id();
      reply = (matched.length ? reply+'\n\n' : '') + '【规则模拟】已按原始输入整理草稿，尚未创建工单。请核对右侧内容。' +
        (missing.length ? `\n建议补充：${missing.join('、')}。` : '\n已识别版本与系统信息，可核对后确认保存。');
    }
    session.history.push({role:'user', content:message, sources:[]}, {role:'assistant', content:reply, sources});
    this.save(state); return clone(session);
  }
  tickets() { return clone(Object.values(this.read().tickets)); }
}
