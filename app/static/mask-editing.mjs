export const DIAMETER_PRESETS = [2, 5, 10, 20, 40];

export function normalizeDiameter(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(1, Math.min(40, Math.round(number * 2) / 2)) : 6;
}

export function createEditSettings() {
  return {brush: {shape: 'circle', diameter: 6}, erase: {shape: 'circle', diameter: 6}};
}

export function strokeSpec(tool, settings) {
  const selected = settings[tool];
  return {radius_mm: normalizeDiameter(selected.diameter) / 2,
    spherical: selected.shape === 'sphere', erase: tool === 'erase'};
}

export function sphereSectionRadius(radius, distance) {
  return Math.abs(distance) > radius ? null : Math.sqrt(Math.max(0, radius ** 2 - distance ** 2));
}
