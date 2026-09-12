export function jobNoduleLabel(job) {return job?.prompt?.nodule_label ?? 1;}

export function selectedNodule(meta, label) {
  return meta?.nodules?.find((nodule) => nodule.label === label) || null;
}

export function proposalIsCurrent(job, meta, label) {
  if (job?.status !== 'succeeded' || jobNoduleLabel(job) !== label) return false;
  const nodule = selectedNodule(meta, label);
  if (meta?.nodules && !nodule) return false;
  return Object.hasOwn(job.prompt || {}, 'nodule_revision_id')
    ? nodule?.mask_revision_id === job.prompt.nodule_revision_id
    : job.base_version === meta?.version;
}

export function previewLabel(savedLabel, proposed, targetLabel) {
  if (targetLabel === null) return savedLabel;
  if (proposed) return savedLabel && savedLabel !== targetLabel ? 256 : 257;
  return savedLabel === targetLabel ? 0 : savedLabel;
}
