views.TradeView = () => {
  const useState = React.useState;
  const [companies, setCompanies] = React.useState([]);
  const [orders, setOrders] = React.useState([]);
  const [form, setForm] = React.useState({
    seller_company_id: "", buyer_company_id: "", year: 2025,
    amount: "", unit_price: "", trade_date: "", remark: "",
  });
  const [msg, setMsg] = React.useState({ type: "", text: "" });
  const [submitting, setSubmitting] = React.useState(false);
  const [filterStatus, setFilterStatus] = useState("");

  const user = window.__user;
  const isEnterprise = user.role === "enterprise";
  const myCompanyId = isEnterprise ? user.company_id : null;
  const companyName = (id) => companies.find((c) => c.id === id)?.name || `企业${id}`;

  const load = () => {
    api.get("/api/trade-orders").then(setOrders).catch(() => {});
  };

  React.useEffect(() => {
    api.get("/api/companies").then(setCompanies).catch(() => {});
    load();
  }, []);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const shown = filterStatus ? orders.filter((o) => o.status === filterStatus) : orders;

  const create = async (e) => {
    e.preventDefault();
    if (!form.seller_company_id || !form.buyer_company_id) {
      setMsg({ type: "err", text: "请选择买方和卖方" });
      return;
    }
    if (Number(form.seller_company_id) === Number(form.buyer_company_id)) {
      setMsg({ type: "err", text: "买方和卖方不能为同一企业" });
      return;
    }
    if (submitting) return;
    setSubmitting(true);
    try {
      await api.post("/api/trade-orders", {
        seller_company_id: Number(form.seller_company_id),
        buyer_company_id: Number(form.buyer_company_id),
        year: Number(form.year),
        amount: Number(form.amount),
        unit_price: form.unit_price ? Number(form.unit_price) : null,
        trade_date: form.trade_date,
        remark: form.remark,
      }, api.idemKey());
      setMsg({ type: "ok", text: "订单已创建，卖方确认即占用配额" });
      setForm({ ...form, amount: "", unit_price: "", trade_date: "", remark: "" });
      load();
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    } finally {
      setSubmitting(false);
    }
  };

  const act = async (o, action, extraBody, confirmText) => {
    if (confirmText && !confirm(confirmText)) return;
    try {
      await api.request("POST", `/api/trade-orders/${o.id}/${action}${qs}`, extraBody || null);
      setMsg({ type: "ok", text: `订单 ${o.order_no} 操作成功` });
      load();
    } catch (err) {
      setMsg({ type: "err", text: err.message });
    }
  };

  const actionButtons = (o) => {
    const iAmSeller = isEnterprise && myCompanyId === o.seller_company_id;
    const iAmBuyer = isEnterprise && myCompanyId === o.buyer_company_id;
    const iAmParty = iAmSeller || iAmBuyer;
    const isRegulator = !isEnterprise && user.role === "admin";

    if (o.status === "pending_confirmation") {
      const btns = [];
      // 未确认的一方出现“确认”按钮
      if (iAmSeller && !o.seller_confirmed) {
        btns.push(html`<button class="btn sm" key="c"
          onClick=${() => act(o, "confirm", null, iAmSeller ? "确认后将占用您的可用配额，是否继续？" : "")}>
          卖方确认</button>`);
      }
      if (iAmBuyer && !o.buyer_confirmed) {
        btns.push(html`<button class="btn sm" key="c" onClick=${() => act(o, "confirm")}>买方确认</button>`);
      }
      if (isRegulator) {
        btns.push(html`<button class="btn sm" key="cs"
          onClick=${() => act(o, `confirm?company_id=${o.seller_company_id}`, null, "代卖方确认？将占用卖方配额")}>代卖方确认</button>`);
      }
      if (iAmParty || isRegulator) {
        btns.push(html`<button class="btn sm ghost" key="x"
          onClick=${() => {
            const reason = prompt("撤销原因（可留空）：", "") || "";
            if (reason === null) return;
            const suffix = isRegulator ? `?company_id=${o.seller_company_id}` : "";
            act(o, `cancel${suffix}`, { reason });
          }}>撤销</button>`);
      }
      return btns;
    }
    if (o.status === "confirmed") {
      if (iAmParty) {
        return [
          html`<button class="btn sm" key="d"
            onClick=${() => act(o, "deliver", null, "确认交割？卖方配额将划转至买方账户")}>交割</button>`,
          html`<button class="btn sm ghost" key="x"
            onClick=${() => {
              const reason = prompt("撤销原因（可留空）：", "") || "";
              if (reason === null) return;
              act(o, "cancel", { reason });
            }}>撤销</button>`,
        ];
      }
      if (isRegulator) {
        return html`<button class="btn sm" onClick=${() => act(o, "deliver", null, "代为交割？")}>代为交割</button>`;
      }
    }
    return null;
  };

  const partyOptions = (exclude) => companies
    .filter((c) => c.status !== "inactive" && c.id !== Number(exclude))
    .map((c) => html`<option key=${c.id} value=${c.id}>${c.name}</option>`);

  return html`
    <div class="panel">
      <h3>发起企业间配额交易</h3>
      <form class="form-grid" onSubmit=${create}>
        <div class="field"><label>卖方企业</label>
          <select value=${form.seller_company_id} onChange=${set("seller_company_id")} required>
            <option value="">请选择卖方</option>
            ${partyOptions(form.buyer_company_id)}
          </select>
        </div>
        <div class="field"><label>买方企业</label>
          <select value=${form.buyer_company_id} onChange=${set("buyer_company_id")} required>
            <option value="">请选择买方</option>
            ${partyOptions(form.seller_company_id)}
          </select>
        </div>
        <div class="field"><label>年度</label>
          <select value=${form.year} onChange=${set("year")}>
            ${[2023, 2024, 2025, 2026].map((y) => html`<option value=${y}>${y}</option>`)}
          </select>
        </div>
        <div class="field"><label>交易数量 (tCO₂)</label>
          <input type="number" step="0.0001" min="0.0001" value=${form.amount} onChange=${set("amount")} required /></div>
        <div class="field"><label>单价 (元/t)</label>
          <input type="number" step="0.01" min="0" value=${form.unit_price} onChange=${set("unit_price")} /></div>
        <div class="field"><label>成交日期</label>
          <input value=${form.trade_date} onChange=${set("trade_date")} placeholder="留空取今天" /></div>
        <div class="field" style=${{gridColumn: "span 2"}}><label>备注</label>
          <input value=${form.remark} onChange=${set("remark")} /></div>
        <div class="actions"><button class="btn" type="submit" disabled=${submitting}>
          ${submitting ? "提交中…" : "创建订单（发起方自动确认本方）"}
        </button></div>
      </form>
    </div>

    <div class="panel">
      <h3>交易订单</h3>
      <div class="filter-bar">
        <div class="field"><label>状态</label>
          <select value=${filterStatus} onChange=${(e) => setFilterStatus(e.target.value)}>
            <option value="">全部</option>
            <option value="pending_confirmation">待双方确认</option>
            <option value="confirmed">双方已确认</option>
            <option value="delivered">已交割</option>
            <option value="cancelled">已撤销</option>
          </select>
        </div>
      </div>
      <table>
        <thead><tr>
          <th>订单号</th><th>年度</th><th>卖方</th><th>买方</th><th>数量 (t)</th><th>单价</th>
          <th>占用 (t)</th><th>状态</th><th>确认</th><th>日期</th><th>操作</th>
        </tr></thead>
        <tbody>
          ${shown.map((o) => html`
            <tr key=${o.id}>
              <td class="mono">${o.order_no}</td>
              <td>${o.year}</td>
              <td>${o.seller_name}${o.seller_confirmed ? " ✓" : " ·"}</td>
              <td>${o.buyer_name}${o.buyer_confirmed ? " ✓" : " ·"}</td>
              <td style=${{fontWeight: "600"}}>${fmtNum(o.amount, 4)}</td>
              <td>${o.unit_price !== null ? fmtNum(o.unit_price) + " 元" : "-"}</td>
              <td>${fmtNum(o.held_amount, 4)}</td>
              <td>${html([TradeStatusBadge(o.status)])}</td>
              <td>${o.seller_confirmed && o.buyer_confirmed ? "双方" : o.seller_confirmed ? "卖方" : o.buyer_confirmed ? "买方" : "-"}</td>
              <td>${o.trade_date || "-"}</td>
              <td>${actionButtons(o)}</td>
            </tr>`)}
          ${shown.length === 0 && html`<tr><td colspan="11" class="empty">暂无交易订单</td></tr>`}
        </tbody>
      </table>
    </div>
    ${msg.text && html`<div class="msg ${msg.type}">${msg.text}</div>`}
  `;
};
