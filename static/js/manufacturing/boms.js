/* ---------- BOM Workflow Manager (2D Canvas) ---------- */
console.log("âœ… BOMS.JS v2.2 LOADED");

function bomNotify(message, type = "info") {
  const text = String(message || "Updated");
  if (typeof window.appNotify === "function") {
    window.appNotify(text, type);
    return;
  }
  if (window.Toast && typeof window.Toast.show === "function") {
    const toastType = type === "error" ? "danger" : type;
    window.Toast.show(text, toastType);
    return;
  }
  window.alert(text);
}

function bomAsk(message, options = {}) {
  if (typeof window.appConfirm === "function") {
    return window.appConfirm(message, options);
  }
  return Promise.resolve(window.confirm(message));
}
let workflowNodes = []; // State: {id, type, name, duration, x, y, machine_id, stage_id}
let draggedNodeIndex = -1; // To track re-ordering or moving
let isDraggingNode = false; // State for dragging existing nodes via mouse
let dragOffsetX = 0;
let dragOffsetY = 0;

function initWorkflow() {
  const canvas = document.getElementById('workflowCanvas');
  const toolbox = document.getElementById('toolboxList');

  if (!canvas || !toolbox) return;

  // Initialize Default Nodes if Empty
  if (workflowNodes.length === 0) {
    addWorkflowNode({ type: 'start', name: 'Production Start', x: 50, y: 150, duration: 0, machine_id: null });
    addWorkflowNode({ type: 'end', name: 'Production Finish', x: 600, y: 150, duration: 0, machine_id: null });
  }

  // --- 1. HTML5 Drop (For NEW items from Toolbox) ---
  canvas.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });

  canvas.addEventListener('drop', (e) => {
    e.preventDefault();
    console.log("Canvas Drop Event Triggered");

    // Only handle Toolbox drops here
    // (Existing nodes use mouse events below)
    const type = e.dataTransfer.getData('type');
    if (!type) return;

    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    console.log("Drop Coords:", x, y);

    const id = e.dataTransfer.getData('id');
    const name = e.dataTransfer.getData('name');
    const machineId = e.dataTransfer.getData('machine-id');

    if (type && id) {
      addWorkflowNode({
        type: type,
        ref_id: id,
        name: name,
        duration: 60,
        x: x - 50,
        y: y - 30,
        machine_id: type === 'machine' ? id : machineId,
        stage_id: type === 'stage' ? id : null
      });
    }
  });

  // --- 2. Mouse Drag (For MOVING existing nodes) ---
  // We attach these to the canvas to handle movement smoothly
  canvas.addEventListener('mousedown', (e) => {
    console.log("Canvas MouseDown");
    const target = e.target.closest('.workflow-node-card');
    if (target) {
      console.log("Target Found:", target);
      isDraggingNode = true;
      draggedNodeIndex = parseInt(target.dataset.index);

      // Calculate offset so it doesn't snap to center
      const rect = target.getBoundingClientRect();
      dragOffsetX = e.clientX - rect.left;
      dragOffsetY = e.clientY - rect.top;
    }
  });

  canvas.addEventListener('mousemove', (e) => {
    if (isDraggingNode && draggedNodeIndex > -1) {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left - dragOffsetX;
      const y = e.clientY - rect.top - dragOffsetY;

      // Update State
      workflowNodes[draggedNodeIndex].x = x;
      workflowNodes[draggedNodeIndex].y = y;

      // Rerender (Optimized: could just move element, but render is fine for now)
      requestAnimationFrame(renderWorkflow);
    }
  });

  window.addEventListener('mouseup', () => {
    if (isDraggingNode) {
      isDraggingNode = false;
      draggedNodeIndex = -1;
    }
  });

  // Toolbox Draggables Setup
  document.querySelectorAll('.toolbox-item').forEach(item => {
    item.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData('type', item.dataset.type);
      e.dataTransfer.setData('id', item.dataset.id);
      e.dataTransfer.setData('name', item.dataset.name);
      if (item.dataset.machineId) e.dataTransfer.setData('machine-id', item.dataset.machineId);
    });
  });

  renderWorkflow();
}

function addWorkflowNode(node) {
  // If no position (e.g. from backend), auto-layout
  if (!node.x) {
    const count = workflowNodes.length;
    node.x = 50 + (count * 150);
    node.y = 100;
  }
  workflowNodes.push(node);
  renderWorkflow();
}

function removeWorkflowNode(index) {
  workflowNodes.splice(index, 1);
  renderWorkflow();
}

function clearWorkflow() {
  workflowNodes = [];
  renderWorkflow();
}

function renderWorkflow() {
  const canvas = document.getElementById('workflowCanvas');
  const svg = document.getElementById('workflowConnections');
  const placeholder = document.getElementById('workflowPlaceholder');

  if (!canvas || !svg) return;

  // Clear Nodes (keep SVG element, remove children nodes)
  // Actually, safer to rebuild HTML content but preserve SVG container reference?
  // Let's clear HTML elements EXCEPT the SVG
  Array.from(canvas.children).forEach(child => {
    if (child.tagName !== 'svg') canvas.removeChild(child);
  });

  // Clear SVG Lines (Keep defs)
  // Remove all lines/paths but keep the <defs>
  const lines = svg.querySelectorAll('line, path');
  lines.forEach(l => l.remove());

  if (workflowNodes.length === 0) {
    if (placeholder) placeholder.classList.remove('hidden');
  } else {
    if (placeholder) placeholder.classList.add('hidden');

    workflowNodes.forEach((node, index) => {
      // Draw Node Card
      const card = document.createElement('div');
      // Absolute positioning + Identifier Class
      card.className = "absolute bg-white border border-gray-200 rounded-md p-2 w-32 shadow-sm flex flex-col gap-1 group hover:border-indigo-500 hover:shadow-md transition cursor-grab z-10 workflow-node-card";
      card.style.left = `${node.x}px`;
      card.style.top = `${node.y}px`;
      card.dataset.index = index; // For Mouse Drag

      // NOTE: We REMOVE draggable="true" here because we are using Mouse Events now
      // card.draggable = true; 
      // card.addEventListener('dragstart', (e) => {
      //   draggedNodeIndex = index;
      //   e.dataTransfer.effectAllowed = "move";
      //   // setTimeout(() => card.classList.add('invisible'), 0); // Optional: hide while dragging
      // });

      card.onclick = () => openNodeEditor(index);

      const stepBadge = `<span class="absolute -top-1.5 -left-1.5 bg-gray-100 text-[9px] text-gray-500 font-bold px-1 rounded border border-gray-200">#${index + 1}</span>`;
      const deleteBtn = `<button onclick="event.stopPropagation(); removeWorkflowNode(${index})" class="absolute -top-2 -right-2 bg-red-100 text-red-600 rounded-full w-5 h-5 flex items-center justify-center shadow-sm hover:bg-red-200 text-xs hidden group-hover:flex">&times;</button>`;

      let innerContent = '';
      if (node.type === 'start') {
        card.className = "absolute bg-green-50 border-2 border-green-200 rounded-full w-32 h-10 flex items-center justify-center shadow-sm z-10 cursor-grab font-bold text-green-700 text-xs uppercase tracking-wide hover:border-green-400 transition workflow-node-card";
        innerContent = `<span>ðŸš€ ${node.name}</span>`;
      } else if (node.type === 'end') {
        card.className = "absolute bg-red-50 border-2 border-red-200 rounded-full w-32 h-10 flex items-center justify-center shadow-sm z-10 cursor-grab font-bold text-red-700 text-xs uppercase tracking-wide hover:border-red-400 transition workflow-node-card";
        innerContent = `<span>ðŸ ${node.name}</span>`;
      } else {
        // Standard Machine/Stage Node
        innerContent = `
                ${stepBadge}
                ${deleteBtn}
                <div class="flex items-center justify-center pt-1 mb-1 pointer-events-none">
                    <span class="text-xl">${node.type === 'machine' ? 'ðŸ­' : 'ðŸ·ï¸'}</span>
                </div>
                <div class="text-center pointer-events-none">
                    <p class="text-[10px] font-bold text-gray-700 truncate w-full">${node.name}</p>
                    <div class="mt-1 inline-block bg-indigo-50 border border-indigo-100 text-indigo-700 text-[10px] font-mono font-bold px-1.5 rounded">
                        ${node.duration}m
                    </div>
                </div>
           `;
      }

      card.innerHTML = innerContent;
      canvas.appendChild(card);

      // Draw Connection to previous node
      if (index > 0) {
        const prev = workflowNodes[index - 1];
        // Simple centroids (approx w-32=128px, h~80px)
        const x1 = prev.x + 64;
        const y1 = prev.y + 40;
        const x2 = node.x + 64;
        const y2 = node.y + 40;

        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', x1);
        line.setAttribute('y1', y1);
        line.setAttribute('x2', x2);
        line.setAttribute('y2', y2);
        line.setAttribute('stroke', '#9CA3AF');
        line.setAttribute('stroke-width', '2');
        line.setAttribute('stroke-dasharray', '4'); // Dashed
        line.setAttribute('marker-end', 'url(#arrowhead)');

        svg.appendChild(line);
      }
    });
  }
}

// Edit Modal Logic
let currentEditIndex = -1;

function openNodeEditor(index) {
  currentEditIndex = index;
  const node = workflowNodes[index];

  const modal = document.getElementById('nodeEditModal');
  document.getElementById('editNodeTitle').textContent = node.name;
  document.getElementById('editNodeDuration').value = node.duration;

  modal.classList.remove('hidden');
}

function closeNodeEditor() {
  document.getElementById('nodeEditModal').classList.add('hidden');
  currentEditIndex = -1;
}

function saveNodeChanges() {
  if (currentEditIndex === -1) return;

  const duration = parseInt(document.getElementById('editNodeDuration').value) || 60;
  workflowNodes[currentEditIndex].duration = duration;

  renderWorkflow();
  closeNodeEditor();
}

function serializeWorkflow() {
  container.innerHTML = '';

  workflowNodes.forEach((node, index) => {
    // Skip Start/End nodes for backend serialization
    if (node.type === 'start' || node.type === 'end') return;

    // We replicate the list inputs the backend expects: op_machines[], op_stages[], op_durations[]
    const machineInput = document.createElement('input');
    machineInput.type = 'hidden';
    machineInput.name = 'op_machines[]';
    machineInput.value = node.machine_id || '';
    container.appendChild(machineInput);

    const stageInput = document.createElement('input');
    stageInput.type = 'hidden';
    stageInput.name = 'op_stages[]';
    stageInput.value = node.stage_id || ''; // Empty if not a stage
    container.appendChild(stageInput);

    const durInput = document.createElement('input');
    durInput.type = 'hidden';
    durInput.name = 'op_durations[]';
    durInput.value = node.duration;
    container.appendChild(durInput);
  });
}

// Populate from Backend Data (reverse of serialize)
function populateWorkflow(opsData) {
  workflowNodes = [];

  if (!opsData || opsData.length === 0) return;

  // TODO: Ideally backend stores X/Y coordinates in BOMOperation model model for true persistence.
  // For now, we auto-layout them linearly if loading from backend.

  opsData.forEach((op, index) => {
    addWorkflowNode({
      type: op.stage_id ? 'stage' : 'machine',
      ref_id: op.stage_id || op.machine_id,
      name: op.stage_name || op.machine_name || `Step`,
      duration: op.duration,
      x: 50 + (index * 150), // Auto-layout Logic
      y: 100,
      machine_id: op.machine_id,
      stage_id: op.stage_id
    });
  });
}


/* ---------- BOM Modal ---------- */
function openBOMModal() {
  document.getElementById("bomModal").classList.remove("hidden");
}
function closeBOMModal() {
  document.getElementById("bomModal").classList.add("hidden");
}

/* ---------- Add / Remove Rows ---------- */
/* ---------- Add / Remove Rows ---------- */
/* ---------- Add / Remove Rows ---------- */
/* ---------- Add / Remove Rows ---------- */
window.addBOMRow = function () {
  console.log("Adding row...");

  const tbody = document.getElementById("bomRows");
  const template = document.getElementById("bomRowTemplate");

  if (!tbody || !template) {
    console.error("Critical: DOM missing bomRows or bomRowTemplate");
    bomNotify("Error: UI Template missing. Please refresh.", "error");
    return;
  }

  const clone = template.content.cloneNode(true);

  // Defaults
  const row = clone.querySelector("tr");
  row.querySelectorAll("input").forEach(i => i.value = (i.name.includes("quantities") ? 1 : ""));
  row.querySelector("select[name='units[]']").value = "kg";
  row.querySelector("select[name='scrap_types[]']").value = "sell_as_scrap";

  // Ensure sub-bom hidden
  const subSel = row.querySelector("select[name='sub_bom_ids[]']");
  if (subSel) subSel.classList.add('hidden');

  tbody.appendChild(clone);
  calculateBOMTotal();
};

/* Event Delegation for Deleting Rows (keep this as is) */
document.addEventListener("click", (e) => {
  // ... (rest of delegation if needed)
});

document.addEventListener("click", (e) => {
  if (e.target.classList.contains("removeRow")) {
    if (document.querySelectorAll("#bomRows tr").length > 1) {
      e.target.closest("tr").remove();
      calculateBOMTotal();
    }
  }
});

/* ---------- Auto Total Calculation ---------- */
function calculateBOMTotal() {
  let totalNet = 0;
  let totalGross = 0;
  let totalScrap = 0;

  const baseQty = parseFloat(document.querySelector('[name="base_quantity"]')?.value) || 1;

  document.querySelectorAll("#bomRows tr").forEach((row) => {
    const qty = parseFloat(row.querySelector('[name="quantities[]"]').value) || 0;
    const cost = parseFloat(row.querySelector('[name="costs[]"]').value) || 0;
    const wasteQty = parseFloat(row.querySelector('[name="wastage[]"]').value) || 0;
    const scrapVal = parseFloat(row.querySelector('[name="scrap_value[]"]').value) || 0;
    const scrapType = row.querySelector('[name="scrap_types[]"]')?.value || "sell_as_scrap";

    const grossLineCost = qty * cost;
    let scrapRecovery = 0;

    // Logic per Scrap Strategy
    if (scrapType === "irretrievable") {
      scrapRecovery = 0; // Cost absorbed
    } else {
      scrapRecovery = wasteQty * scrapVal; // Sell or Return recovers value
    }

    // Validation Visuals
    const rowEl = row.closest('tr'); // redundant, row is tr
    if (qty <= 0) row.querySelector('[name="quantities[]"]').classList.add("border-red-500", "bg-red-50");
    else row.querySelector('[name="quantities[]"]').classList.remove("border-red-500", "bg-red-50");

    if (wasteQty >= qty) row.querySelector('[name="wastage[]"]').classList.add("border-red-500", "ring-2", "ring-red-500");
    else row.querySelector('[name="wastage[]"]').classList.remove("border-red-500", "ring-2", "ring-red-500");

    // Ensure recovery doesn't exceed cost (Net shouldn't be negative unless specific use case)
    // Formula: Net = Gross - Scrap Recovery
    let netLineCost = grossLineCost - scrapRecovery;

    if (netLineCost < 0) netLineCost = 0; // Prevent negative cost

    totalGross += grossLineCost;
    totalScrap += scrapRecovery;
    totalNet += netLineCost;
  });

  // Calculate Cost Per Unit
  const unitCost = baseQty > 0 ? (totalNet / baseQty) : 0;

  // Update UI
  const dispGross = document.getElementById("dispGrossCost");
  const dispScrap = document.getElementById("dispScrapRecovery");
  const dispNet = document.getElementById("dispNetCost");
  const dispUnitCost = document.getElementById("dispUnitCost");
  const dispTotal = document.getElementById("bomTotalCost");

  if (dispGross) dispGross.textContent = totalGross.toFixed(2);
  if (dispScrap) dispScrap.textContent = totalScrap.toFixed(2);
  if (dispNet) dispNet.textContent = totalNet.toFixed(2);
  if (dispUnitCost) dispUnitCost.textContent = unitCost.toFixed(2);
  if (dispTotal) dispTotal.textContent = totalNet.toFixed(2);
}

document.addEventListener("input", (e) => {
  if (e.target.matches('[name="quantities[]"], [name="costs[]"], [name="wastage[]"], [name="scrap_value[]"], [name="base_quantity"], select')) {
    calculateBOMTotal();
  }
});

function refreshBOMs() {
  location.reload();
}

/* ---------- Save BOM ---------- */
/* ---------- Save BOM ---------- */
/* ---------- Save BOM ---------- */
/* ---------- Save BOM ---------- */
function submitBOM() {
  document.getElementById('createBOMForm').requestSubmit();
}

document.addEventListener("submit", async (e) => {
  if (e.target && e.target.id === "createBOMForm") {
    e.preventDefault();

    // 1. Validate Base Quantity
    const baseQtyInput = document.querySelector('[name="base_quantity"]');
    const baseQty = parseFloat(baseQtyInput.value);
    if (!baseQty || baseQty <= 0) {
      bomNotify("Base Batch Size must be greater than 0.", "warning");
      baseQtyInput.focus();
      return;
    }

    // 2. Validate Rows
    const rows = document.querySelectorAll("#bomRows tr");
    if (rows.length === 0) {
      bomNotify("Please add at least one material.", "warning");
      return;
    }

    let isValid = true;
    for (const row of rows) {
      const material = row.querySelector('[name="materials[]"]').value.trim();
      const qty = parseFloat(row.querySelector('[name="quantities[]"]').value) || 0;
      const wastage = parseFloat(row.querySelector('[name="wastage[]"]').value) || 0;

      if (!material) {
        bomNotify("Material Name is required.", "warning");
        row.querySelector('[name="materials[]"]').focus();
        isValid = false; break;
      }

      if (qty <= 0) {
        bomNotify(`Quantity for '${material}' must be greater than 0.`, "warning");
        row.querySelector('[name="quantities[]"]').focus();
        isValid = false; break;
      }

      if (wastage >= qty) {
        bomNotify(`Wastage (${wastage}) cannot be greater than or equal to Quantity (${qty}) for '${material}'.`, "warning");
        row.querySelector('[name="wastage[]"]').focus();
        isValid = false; break;
      }
    }

    if (!isValid) return;

    if (!isValid) return;

    // SERIALIZE WORKFLOW BEFORE SUBMIT
    serializeWorkflow();

    // Proceed if Valid
    const formData = new FormData(e.target);

    // Debug: Log values
    // for (let [key, value] of formData.entries()) { console.log(key, value); }

    try {
      const res = await fetch("/manufacturing/create-bom/", {
        method: "POST",
        body: formData,
      });
      const data = await res.json();
      if (data.success) {
        bomNotify("BOM saved/created successfully.", "success");
        closeBOMModal();
        refreshBOMs();
      } else {
        bomNotify("Error: " + data.error, "error");
      }
    } catch (err) {
      bomNotify("Server error: " + err, "error");
    }
  }
});



// -------- Timeline Refresh --------
async function refreshTimeline() {
  const container = document.getElementById("timelineContainer");
  container.innerHTML = "<p class='text-gray-400 italic'>â³ Loading timeline...</p>";
  try {
    const response = await fetch("/manufacturing/create-workorder/?ajax=timeline");

    const data = await response.json();
    if (data.success) {
      container.innerHTML = data.html;
      // Initialize Chart if available
      if (window.initGanttChart) window.initGanttChart(true);
    } else {
      container.innerHTML = `<p class='text-red-500 text-sm'>âŒ ${data.error}</p>`;
    }
  } catch (e) {
    console.error(e);
    container.innerHTML = "<p class='text-red-500 text-sm'>âš ï¸ Failed to load timeline.</p>";
  }
}


setInterval(refreshTimeline, 30000); // refresh every 30 seconds

/* ---------- TEST / SIMULATION MODE ---------- */
// Old simulation logic removed. See bottom of file for updated version.

function populateBOMModal(bom) {
  // Fill Header
  document.querySelector('[name="product_name"]').value = bom.product_name;
  document.querySelector('[name="version"]').value = bom.version;
  document.querySelector('[name="base_quantity"]').value = bom.base_quantity;
  const uomSelect = document.querySelector('[name="uom"]');
  if (uomSelect) uomSelect.value = bom.uom || 'pcs';

  document.querySelector('[name="status"]').value = bom.status;

  const tbody = document.getElementById("bomRows");
  tbody.innerHTML = ""; // Clear existing

  const template = document.getElementById("bomRowTemplate");

  bom.components.forEach(comp => {
    const clone = template.content.cloneNode(true);
    const row = clone.querySelector("tr");

    row.querySelector('[name="materials[]"]').value = comp.material_name;
    row.querySelector('[name="quantities[]"]').value = comp.quantity;
    row.querySelector('[name="units[]"]').value = comp.unit;
    row.querySelector('[name="costs[]"]').value = comp.cost_per_unit;
    row.querySelector('[name="wastage[]"]').value = comp.wastage_quantity;
    row.querySelector('[name="scrap_value[]"]').value = comp.scrap_value_per_unit;
    row.querySelector('[name="types[]"]').value = comp.source_type;
    row.querySelector('[name="scrap_types[]"]').value = comp.scrap_type;

    // Handle Sub-BOM linkage in simulation
    const subSel = row.querySelector("select[name='sub_bom_ids[]']");
    if (subSel) {
      if (comp.source_type === 'semi_finished') {
        subSel.classList.remove('hidden');
        // We can't easily pre-select if we don't have the ID in the JSON
        // Assuming the server returns it (needs update in get_bom_json view)
        // For now, if we match by name logic? 
        // Ideally Update GET_BOM_JSON to return sub_bom_id
        if (comp.sub_bom_id) subSel.value = comp.sub_bom_id;
      } else {
        subSel.classList.add('hidden');
      }
    }

    tbody.appendChild(clone);
  });

  // Populate Operations (Visual Workflow)
  if (bom.operations && bom.operations.length > 0) {
    populateWorkflow(bom.operations);
  } else {
    clearWorkflow();
  }

  // If no components, add one empty
  if (bom.components.length === 0) {
    const clone = template.content.cloneNode(true);
    // Set defaults
    const row = clone.querySelector("tr");
    row.querySelector('[name="quantities[]"]').value = 1;
    tbody.appendChild(clone);
  }
  // Set Status Dropdown
  const statusSelect = document.getElementById("bomStatusSelect");
  if (statusSelect) {
    statusSelect.value = bom.status || 'draft';
  }

  // Show 'Save New Ver' if editing existing BOM
  // User requested to hide this explicit button.
  const btnSaveCopy = document.getElementById("btnSaveCopy");
  if (btnSaveCopy) btnSaveCopy.classList.add("hidden");
}

// Ensure Global Availability
window.openBOMSimulation = openBOMSimulation;

window.submitAsNewVersion = async function () {
  const confirmed = await bomAsk("Create a NEW version initialized with this data?", {
    title: "Create New BOM Version",
    confirmText: "Create Version",
    cancelText: "Cancel"
  });
  if (!confirmed) return;

  // Trick: clear the BOM ID so backend treats it as new
  const idField = document.getElementById("simBomId");
  const originalId = idField.value;
  idField.value = ""; // Clear

  // Set status to draft mostly
  const statusSelect = document.getElementById("bomStatusSelect");
  const originalStatus = statusSelect.value;
  if (statusSelect) statusSelect.value = "draft";

  // Submit
  submitBOM();

  // Attempt restore UI state (though modal likely closes)
  setTimeout(() => {
    idField.value = originalId;
    if (statusSelect) statusSelect.value = originalStatus;
  }, 2000);
};

function resetModalState() {
  document.getElementById("testModeBanner").classList.add("hidden");
  document.getElementById("addRow").style.display = "block";
  document.getElementById("modalTitle").textContent = "Create Bill of Materials";
  document.getElementById("createBOMForm").reset();

  const btnSaveCopy = document.getElementById("btnSaveCopy");
  if (btnSaveCopy) btnSaveCopy.classList.add("hidden");

  const statusSelect = document.getElementById("bomStatusSelect");
  if (statusSelect) statusSelect.value = 'draft';

  // Reset Inputs
  const inputs = document.querySelectorAll("#createBOMForm input, #createBOMForm select");
  inputs.forEach(inp => inp.disabled = false);

  // Clear rows but keep one
  const tbody = document.getElementById("bomRows");
  tbody.innerHTML = "";

  const template = document.getElementById("bomRowTemplate");
  const clone = template.content.cloneNode(true);
  const row = clone.querySelector("tr");
  row.querySelector('[name="quantities[]"]').value = 1;
  tbody.appendChild(clone);

  clearWorkflow(); // Reset Visual Editor
  calculateBOMTotal();
}

window.openBOMModal = function (reset = false) {
  if (reset) resetModalState();
  document.getElementById("bomModal").classList.remove("hidden");
  setTimeout(initWorkflow, 100);
}

// NOTE: activateBOM and updateBOMStatus are replaced by the Unified Save/Update Button
// which submits the 'status' dropdown value along with the form.
// Lifecycle Management
// Lifecycle Management
async function updateBOMStatus(newStatus) {
  const bomId = document.getElementById("simBomId").value;
  if (!bomId) return;

  // SPECIAL CASE: Submitting to Test needs to Save Changes first
  if (newStatus === 'test' || newStatus === 'draft') {
    // Set the hidden status input specifically in the BOM form
    const statusInput = document.querySelector('#createBOMForm [name="status"]');
    if (statusInput) statusInput.value = newStatus;

    const shouldSave = await bomAsk(`Save changes and set status to '${newStatus}'?`, {
      title: "Confirm Status Change",
      confirmText: "Save",
      cancelText: "Cancel"
    });
    if (shouldSave) {
      submitBOM(); // This submits form to create_bom, which now handles updates
    }
    return;
  }

  // Active/Archive logic remains separate (no form data needed usually, but safety check)
  let confirmMsg = "";
  if (newStatus === 'archived') confirmMsg = "ðŸ“¦ Archive this BOM? It will be hidden.";

  if (confirmMsg) {
    const confirmed = await bomAsk(confirmMsg, {
      title: "Confirm Status Change",
      confirmText: "Archive",
      cancelText: "Cancel"
    });
    if (!confirmed) return;
  }

  try {
    const res = await fetch("/manufacturing/update-bom-status/", {
      method: "POST",
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: `bom_id=${bomId}&status=${newStatus}`
    });
    const data = await res.json();
    if (data.success) {
      bomNotify(data.message, "success");
      closeBOMModal();
      refreshBOMs();
    } else {
      bomNotify(data.error || "Failed to update BOM status.", "error");
    }
  } catch (err) {
    bomNotify("Error: " + err, "error");
  }
}

/* ---------- TEST / SIMULATION MODE ---------- */
async function openBOMSimulation(bomId) {
  try {
    const res = await fetch(`/manufacturing/get-bom-json/${bomId}/`);
    const data = await res.json();

    if (data.success) {
      populateBOMModal(data.bom);

      const status = data.bom.status;
      const isDraft = status === 'draft';
      const isTest = status === 'test';
      const isActive = status === 'active';
      const isArchived = status === 'archived';

      openBOMModal(); // Show modal

      // 1. Title & Banner
      document.getElementById("modalTitle").textContent = isDraft ? "Edit Bill of Materials" : `BOM: ${data.bom.product_name}`;
      document.getElementById("simBomId").value = bomId;

      const banner = document.getElementById("testModeBanner");
      banner.classList.remove("hidden", "bg-yellow-100", "text-yellow-800", "border-yellow-300", "bg-green-100", "text-green-800", "border-green-300", "bg-red-100", "text-red-800", "border-red-300");

      if (isTest) {
        banner.classList.add("bg-yellow-100", "text-yellow-800", "border-yellow-300");
        banner.textContent = "âš ï¸ Test Simulation Mode (Read-Only)";
        banner.classList.remove("hidden");
      } else if (isActive) {
        banner.classList.add("bg-green-100", "text-green-800", "border-green-300");
        banner.textContent = "ðŸŸ¢ Active BOM (Production Ready)";
        banner.classList.remove("hidden");
      } else if (isArchived) {
        banner.classList.add("bg-red-100", "text-red-800", "border-red-300");
        banner.textContent = "ðŸ”´ Archived BOM (Read Only)";
        banner.classList.remove("hidden");
      } else {
        banner.classList.add("hidden");
      }

      // 2. Button Visibility (Unified Save Button handles all)
      // Just ensure the Save button is visible.
      const btnSave = document.querySelector("#bomModal button[onclick='submitBOM()']"); // Or use class/ID if available
      // Actually we have a generic footer now.

      // 3. Inputs Lock based on status
      const inputs = document.querySelectorAll("#createBOMForm input, #createBOMForm select");

      if (isDraft) {
        inputs.forEach(inp => inp.disabled = false);
        // Enable Add/Remove rows
        const addRow = document.getElementById("addRow");
        if (addRow) addRow.style.display = "block";
        document.querySelectorAll(".removeRow").forEach(b => b.style.display = "inline");
      } else {
        // Read Only (Test, Active, Archived)
        inputs.forEach(inp => {
          if (inp.type === 'hidden') return;
          // Allow status change for Active/Test if needed (e.g. to Archive)
          // But generally status change logic implies we allow editing the status dropdown?
          // The status dropdown is inside #createBOMForm? Yes.
          if (inp.id === 'bomStatusSelect') {
            inp.disabled = false; // Allow changing status
          } else {
            inp.disabled = true;
          }
        });

        const addRow = document.getElementById("addRow");
        if (addRow) addRow.style.display = "none";
        document.querySelectorAll(".removeRow").forEach(b => b.style.display = "none");
      }

      calculateBOMTotal();

      calculateBOMTotal();
    } else {
      bomNotify("Server Error: " + data.error, "error");
    }
  } catch (err) {
    console.error(err);
    bomNotify("Failed to load BOM: " + err.message, "error");
  }
}

