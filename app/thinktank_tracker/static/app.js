document.addEventListener("DOMContentLoaded", () => {
  if (window.lucide) window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
});
document.body.addEventListener("htmx:afterSwap", () => {
  if (window.lucide) window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
});

document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-confirm]");
  if (trigger && !window.confirm(trigger.dataset.confirm)) event.preventDefault();
});

document.querySelectorAll(".review-form").forEach((form) => {
  const decision = form.querySelector('select[name="decision"]');
  const requiredForInclude = form.querySelectorAll("[data-required-for-include]");
  const syncRequirements = () => {
    const approving = decision?.value === "include";
    requiredForInclude.forEach((field) => {
      field.required = approving;
      field.setAttribute("aria-required", approving ? "true" : "false");
    });
  };
  decision?.addEventListener("change", syncRequirements);
  syncRequirements();
});

const providerSelect = document.querySelector("#provider-kind");
providerSelect?.addEventListener("change", () => {
  const option = providerSelect.selectedOptions[0];
  const baseUrl = document.querySelector("#api-base-url");
  const modelName = document.querySelector("#model-name");
  const apiStyle = document.querySelector("#api-style");
  if (baseUrl && option.dataset.baseUrl) baseUrl.value = option.dataset.baseUrl;
  if (modelName && option.dataset.model) modelName.value = option.dataset.model;
  if (apiStyle && option.dataset.apiStyle) apiStyle.value = option.dataset.apiStyle;
  if (apiStyle) apiStyle.disabled = providerSelect.value === "deepseek";
});
if (providerSelect?.value === "deepseek") {
  const apiStyle = document.querySelector("#api-style");
  if (apiStyle) apiStyle.disabled = true;
}

const ruleDialog = document.querySelector("[data-rule-dialog]");
const resetRuleDialog = () => {
  const form = ruleDialog?.querySelector("form");
  form?.reset();
  if (form) form.querySelector('input[name="rule_id"]').value = "";
  const title = ruleDialog?.querySelector("[data-rule-dialog-title]");
  if (title) title.textContent = "新增体裁规则";
};
document.querySelector("[data-open-rule-dialog]")?.addEventListener("click", () => {
  resetRuleDialog();
  ruleDialog?.showModal();
  ruleDialog?.querySelector('input[name="match_value"]')?.focus();
});
document.querySelectorAll("[data-edit-rule]").forEach((button) => {
  button.addEventListener("click", () => {
    resetRuleDialog();
    const form = ruleDialog?.querySelector("form");
    if (!form) return;
    form.querySelector('input[name="rule_id"]').value = button.dataset.ruleId || "";
    form.querySelector('[name="match_kind"]').value = button.dataset.matchKind || "card_label";
    form.querySelector('[name="match_value"]').value = button.dataset.matchValue || "";
    form.querySelector('[name="content_type"]').value = button.dataset.contentType || "";
    form.querySelector('[name="action"]').value = button.dataset.action || "review";
    const title = ruleDialog.querySelector("[data-rule-dialog-title]");
    if (title) title.textContent = "编辑体裁规则";
    ruleDialog.showModal();
  });
});
document.querySelectorAll("[data-close-rule-dialog]").forEach((button) => {
  button.addEventListener("click", () => ruleDialog?.close());
});

const candidateRuleDialog = document.querySelector("[data-candidate-rule-dialog]");
document.querySelectorAll("[data-candidate-rule-label]").forEach((button) => {
  button.addEventListener("click", () => {
    const label = button.dataset.candidateRuleLabel || "";
    const rawInput = candidateRuleDialog?.querySelector('input[name="raw_label"]');
    const typeInput = candidateRuleDialog?.querySelector('input[name="content_type"]');
    if (rawInput) rawInput.value = label;
    if (typeInput) typeInput.value = label;
    candidateRuleDialog?.showModal();
    typeInput?.focus();
  });
});
document.querySelectorAll("[data-close-candidate-rule-dialog]").forEach((button) => {
  button.addEventListener("click", () => candidateRuleDialog?.close());
});

const syncCustomType = (select) => {
  const form = select.closest("form");
  const field = form?.querySelector(".custom-type-field");
  const input = field?.querySelector('input[name="custom_content_type"]');
  const visible = select.value === "__custom__";
  field?.classList.toggle("is-hidden", !visible);
  if (input) input.required = visible && form?.querySelector('select[name="decision"]')?.value === "include";
};
document.querySelectorAll("[data-custom-type-select]").forEach(syncCustomType);
document.querySelectorAll('.review-form select[name="decision"]').forEach((decision) => {
  decision.addEventListener("change", () => {
    const select = decision.closest("form")?.querySelector("[data-custom-type-select]");
    if (select) syncCustomType(select);
  });
});
document.addEventListener("change", (event) => {
  if (event.target.matches("[data-custom-type-select]")) syncCustomType(event.target);
});

document.addEventListener("click", (event) => {
  const apply = event.target.closest("[data-apply-suggestion]");
  if (!apply) return;
  const panel = apply.closest("[data-model-suggestion]");
  const form = document.querySelector("#review-form");
  if (!panel || !form) return;
  panel.querySelectorAll("[data-suggestion-field]:checked").forEach((checkbox) => {
    const fieldName = checkbox.dataset.suggestionField;
    const value = checkbox.closest(".suggestion-field")?.querySelector("[data-suggestion-value]")?.value ?? "";
    if (fieldName === "secondary_categories") {
      let selected = [];
      try { selected = JSON.parse(value); } catch (_error) { selected = []; }
      form.querySelectorAll('input[name="secondary_categories"]').forEach((input) => {
        input.checked = selected.includes(input.value);
      });
      return;
    }
    if (fieldName === "content_type") {
      const select = form.querySelector('[data-review-field="content_type"]');
      const option = Array.from(select?.options ?? []).find((item) => item.value === value);
      if (select) select.value = option ? value : "__custom__";
      const custom = form.querySelector('[data-review-field="custom_content_type"]');
      if (custom) custom.value = option ? "" : value;
      if (select) syncCustomType(select);
      return;
    }
    const target = form.querySelector(`[data-review-field="${fieldName}"]`);
    if (target) {
      target.value = value;
      target.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });
  apply.textContent = "已应用到表单";
  if (window.lucide) window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
});

const loadModelsButton = document.querySelector("#load-models");
loadModelsButton?.addEventListener("click", async () => {
  const status = document.querySelector("#model-list-status");
  loadModelsButton.disabled = true;
  if (status) status.textContent = "正在读取模型列表…";
  try {
    const response = await fetch("/settings/models", { headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    const list = document.querySelector("#available-models");
    if (list) {
      list.replaceChildren();
      payload.models.forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        list.append(option);
      });
    }
    if (status) status.textContent = `已读取 ${payload.models.length} 个可用模型`;
  } catch (error) {
    if (status) status.textContent = error.message;
  } finally {
    loadModelsButton.disabled = false;
  }
});
