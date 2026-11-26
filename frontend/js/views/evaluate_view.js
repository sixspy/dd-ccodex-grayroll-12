import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const EvaluateView = {
  setup() {
    const form = ref({ flag_key: "checkout.new_pay", env_code: "prod", subject_key: "u-1001" });
    const result = ref(null); const board = ref(null); const err = ref("");
    const pendingDetail = ref(null);
    async function reloadBoard() { board.value = await api("/api/evaluate/board"); }
    async function run() {
      err.value = ""; pendingDetail.value = null; result.value = null;
      try { result.value = await api("/api/evaluate", { method: "POST", body: JSON.stringify(form.value) }); }
      catch (e) {
        // api.js 已把结构化 detail JSON 字符串化；这里尽量还原 rename_shadow_pending
        const msg = e.message || String(e);
        err.value = msg;
        try {
          const parsed = JSON.parse(msg);
          if (parsed && parsed.code === "rename_shadow_pending") pendingDetail.value = parsed;
        } catch { /* 普通字符串 detail */ }
      }
    }
    onMounted(reloadBoard);
    return { form, result, board, err, pendingDetail, run, reloadBoard };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>在线求值</h2>
      <div class="field"><label>开关键</label><input v-model="form.flag_key" /></div>
      <div class="field"><label>环境</label>
        <select v-model="form.env_code"><option>dev</option><option>staging</option><option>prod</option></select>
      </div>
      <div class="field"><label>主体键</label><input v-model="form.subject_key" /></div>
      <button class="btn" @click="run">求值</button>
      <div class="err" v-if="err && !pendingDetail">{{ err }}</div>
      <div class="err" v-if="pendingDetail" style="white-space:normal">
        <strong>假绿关：新键尚未升正，求值读口未切换。</strong>
        <div>{{ pendingDetail.message }}</div>
        <div v-if="pendingDetail.pending_rename">
          影子票 #{{ pendingDetail.pending_rename.order_id }}：
          <code>{{ pendingDetail.pending_rename.old_key }}</code> →
          <code>{{ pendingDetail.pending_rename.new_key }}</code>
          （{{ pendingDetail.pending_rename.status }}）
        </div>
        <div class="muted">请前往「开关」页将该迁移票原子升正后，再用新键求值。</div>
      </div>
      <div v-if="result && result.pending_rename" class="hint" style="border-left:3px solid #d29922;padding-left:8px;margin:0.6rem 0">
        该键有未升正更名影子：<code>{{ result.pending_rename.old_key }}</code> →
        <code>{{ result.pending_rename.new_key }}</code>，当前仍按旧键正式规则求值；升正后读口才切换。
      </div>
      <pre v-if="result" style="margin-top:0.8rem">{{ result }}</pre>
    </div>
    <div class="panel span-7">
      <h2>正式规则板</h2>
      <table v-if="board">
        <thead><tr><th>开关</th><th>环境</th><th>类型</th><th>%</th><th>版本</th></tr></thead>
        <tbody>
          <tr v-for="(r,i) in board.rules" :key="i">
            <td>{{ r.flag_key }}</td><td>{{ r.env_code }}</td><td>{{ r.kind }}</td><td>{{ r.percent }}</td><td>v{{ r.version }}</td>
          </tr>
        </tbody>
      </table>
      <h2 style="margin-top:1rem">待升正更名影子</h2>
      <table v-if="board && board.pending_renames && board.pending_renames.length">
        <thead><tr><th>票</th><th>旧键</th><th>新键</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="p in board.pending_renames" :key="p.id">
            <td>#{{ p.id }}</td><td><code>{{ p.old_key }}</code></td><td><code>{{ p.new_key }}</code></td><td>{{ p.status }}</td>
          </tr>
        </tbody>
      </table>
      <p class="muted" v-else>暂无待升正更名影子。</p>
      <button class="btn secondary" style="margin-top:0.7rem" @click="reloadBoard">刷新</button>
    </div>
  </div>`
};
