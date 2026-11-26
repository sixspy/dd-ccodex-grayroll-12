import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const FlagsView = {
  setup() {
    const items = ref([]);
    const orders = ref([]);
    const form = ref({ key: "promo.banner_v3", title: "活动横幅 v3", description: "首页横幅实验", owner: "writer" });
    const renameDrafts = ref({});
    const err = ref("");
    const msg = ref("");

    async function reload() {
      const [flags, rs] = await Promise.all([
        api("/api/flags"),
        api("/api/flags/rename-migrations").catch(() => ({ items: [] })),
      ]);
      items.value = (flags.items || []).map((it) => ({ ...it, _newKey: "", _mode: "jia", _editing: false }));
      orders.value = rs.items || [];
    }
    async function create() {
      err.value = ""; msg.value = "";
      try {
        await api("/api/flags", { method: "POST", body: JSON.stringify(form.value) });
        msg.value = "开关已登记";
        await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    function startRename(it) {
      err.value = ""; msg.value = "";
      it._editing = true;
      it._newKey = it.key + "_v2";
    }
    function cancelRename(it) { it._editing = false; }
    async function submitRename(it) {
      err.value = ""; msg.value = "";
      const useShadow = it._mode === "yi";
      try {
        const res = await api("/api/flags/rename-migrate", {
          method: "POST",
          body: JSON.stringify({ flag_id: it.id, new_key: it._newKey, use_shadow: useShadow }),
        });
        msg.value = useShadow
          ? `已落影子迁移票 #${res.ticket_id}：正式键与外键零改，待原子升正`
          : `已同事务更名并迁移外键（票 #${res.ticket_id}，${Object.values(res.migrated || {}).reduce((a, b) => a + b, 0)} 行）`;
        await reload();
      } catch (e) { err.value = e.message || String(e); await reload(); }
    }
    async function promote(order) {
      err.value = ""; msg.value = "";
      try {
        const res = await api(`/api/flags/rename-shadow/${order.id}/promote`, { method: "POST", body: "{}" });
        msg.value = `票 #${order.id} 已原子升正，外键同票迁完（${Object.values(res.migrated || {}).reduce((a, b) => a + b, 0)} 行）`;
        await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    onMounted(reload);
    return { items, orders, form, err, msg, create, reload, startRename, cancelRename, submitRename, promote };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>登记开关</h2>
      <div class="field"><label>开关键</label><input v-model="form.key" /></div>
      <div class="field"><label>标题</label><input v-model="form.title" /></div>
      <div class="field"><label>说明</label><textarea v-model="form.description" rows="2"></textarea></div>
      <button class="btn" @click="create">创建</button>
      <div class="muted" v-if="msg" style="color:#1a7f37">{{ msg }}</div>
      <div class="err" v-if="err">{{ err }}</div>
    </div>
    <div class="panel span-7">
      <h2>开关列表 · 键更名</h2>
      <p class="hint">甲路径：更名与规则/发布等外键同一事务迁完，失败整笔回滚；乙路径：先落影子迁移草案，正式外键零改，升正时原子切换求值读口。</p>
      <table>
        <thead><tr><th>ID</th><th>键</th><th>标题</th><th>状态</th><th>更名</th></tr></thead>
        <tbody>
          <tr v-for="it in items" :key="it.id">
            <td>{{ it.id }}</td>
            <td><code>{{ it.key }}</code></td>
            <td>{{ it.title }}</td>
            <td><span class="pill" :class="{'danger': it.status==='killed'}">{{ it.status_label || it.status }}</span></td>
            <td>
              <button class="btn secondary" v-if="!it._editing" @click="startRename(it)">更名</button>
              <div v-else style="min-width:260px">
                <input v-model="it._newKey" placeholder="新键，如 ads.bid_curve_v2" />
                <select v-model="it._mode" style="margin-top:4px">
                  <option value="jia">甲：同事务迁移外键</option>
                  <option value="yi">乙：影子草案待升正</option>
                </select>
                <div style="margin-top:4px">
                  <button class="btn" @click="submitRename(it)">提交更名</button>
                  <button class="btn secondary" @click="cancelRename(it)">取消</button>
                </div>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel span-12">
      <h2>键更名迁移票（同票迁移 · 跨表账实）</h2>
      <table v-if="orders.length">
        <thead><tr><th>票</th><th>旧键 → 新键</th><th>路径</th><th>状态</th><th>外键账实</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="o in orders" :key="o.id">
            <td>#{{ o.id }}</td>
            <td><code>{{ o.old_key }}</code> → <code>{{ o.new_key }}</code></td>
            <td>{{ o.path === 'yi' ? '乙 · 影子' : '甲 · 同事务' }}</td>
            <td><span class="pill" :class="{'danger': o.status==='open'}">{{ o.status }}</span></td>
            <td class="muted">{{ Object.entries(o.migrated || {}).map(([k,v]) => k+':'+v).join('  ') || '正式外键零改（待升正）' }}</td>
            <td><button class="btn" v-if="o.status==='open'" @click="promote(o)">原子升正</button></td>
          </tr>
        </tbody>
      </table>
      <p class="muted" v-else>暂无更名迁移票。</p>
      <button class="btn secondary" style="margin-top:0.7rem" @click="reload">刷新</button>
    </div>
  </div>`
};
