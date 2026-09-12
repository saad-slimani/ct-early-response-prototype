export function featureFileDialog({state, api, escape, showModal, refresh, action}) {
  const families = ['shape', 'firstorder', 'glcm', 'glrlm', 'glszm', 'gldm', 'ngtdm'];
  return async function openFeatureFile(id) {
    await refresh(false);
    const row = state.data.assignments.find(item => item.id === id);
    if (row?.status !== 'approved') throw new Error('Independent approval is required before generating a feature file.');
    const mr = row.modality === 'MR';
    showModal('Feature file', `<p><strong>${escape(row.patient_id)}</strong> / approved revision ${row.revision_id.slice(0, 8)}</p>
      <form id="feature-form" data-study="${id}">
        <fieldset><legend>Original-image feature families</legend><div class="feature-families">${families.map(name => `<label><input type="checkbox" name="families" value="${name}" checked> ${name === 'firstorder' ? 'First order' : name.toUpperCase()}</label>`).join('')}</div></fieldset>
        <div class="feature-settings"><label class="form-label">Bin width<input name="bin_width" type="number" min="1" max="1000" step="any" value="${mr ? 20 : 25}" required></label>
        <label class="form-label">Resampling<select name="resample_mm"><option value="">Native grid</option><option value="1">1 mm isotropic</option><option value="2">2 mm isotropic</option><option value="3">3 mm isotropic</option></select></label>
        <label class="form-label">Intensity normalization<select name="normalize"><option value="false" ${mr ? '' : 'selected'}>None (retain CT HU)</option><option value="true" ${mr ? 'selected' : ''}>Whole-volume z-score</option></select></label>
        <label class="form-label">Z-score scale<input name="normalize_scale" type="number" min="1" max="1000" value="100" required></label></div>
        <p class="small-note">One row per lesion. The ZIP includes features.csv, reusable PyRadiomics settings and image/mask hashes with reviewer and revision provenance. These settings are not a validated biomarker protocol.</p>
        <button class="primary" id="generate-features">Generate file</button>
      </form><div id="feature-files" aria-live="polite"></div>`);
    const form = document.getElementById('feature-form');
    const stillOpen = () => document.getElementById('modal').open && document.getElementById('feature-form') === form;
    const listFiles = async () => {
      const jobs = await api(`/api/radiomics?study_id=${id}`);
      if (!stillOpen()) return;
      document.getElementById('feature-files').innerHTML = jobs.length ? `<h3>Generated files</h3>${jobs.map(job => `<div class="feature-file"><span>${escape(job.protocol.id)}<small>${escape(job.stale ? 'Approval superseded' : job.status)}${job.error ? ': ' + escape(job.error) : ''}</small></span>${job.status === 'succeeded' && !job.stale ? `<a class="primary" href="/api/radiomics/${job.id}/export?format=zip">Download ZIP</a><a href="/api/radiomics/${job.id}/export">CSV</a>` : ''}</div>`).join('')}` : '';
      if (jobs.some(job => ['queued', 'running'].includes(job.status))) setTimeout(() => listFiles().catch(() => {}), 2000);
    };
    form.onsubmit = action(async event => {
      event.preventDefault();
      const data = new FormData(form), selected = data.getAll('families');
      if (!selected.length) throw new Error('Select at least one feature family.');
      const button = document.getElementById('generate-features');
      button.disabled = true;
      try {
        await api(`/api/assignments/${id}/radiomics`, {version: row.version, revision_id: row.revision_id,
          settings: {families: selected, bin_width: Number(data.get('bin_width')), normalize: data.get('normalize') === 'true',
            normalize_scale: Number(data.get('normalize_scale')), resample_mm: data.get('resample_mm') ? Number(data.get('resample_mm')) : null}});
        await listFiles();
      } finally { button.disabled = false; }
    });
    await listFiles();
  };
}
