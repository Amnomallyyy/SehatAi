// 3D scan visual for the login page's hero panel. Plain DOM generation,
// no framework, no build step -- ported 1:1 from the design reference's
// React component (scanEl/ring): a stack of rotated rings whose diameter
// follows a fixed silhouette profile, spun together around the vertical
// axis inside a perspective container, reads as a rotating body scan.
// Standalone file (not part of app.js) since nothing else depends on it.

const SCAN_PROFILE = [
  34, 44, 50, 48, 40, 26, 24, 60, 86, 100, 104, 104, 100, 96, 86, 74, 70, 78,
  88, 90, 82, 72, 62, 52, 44, 38
];

function buildScanRing(diameter, index, total) {
  const y = -150 + index * (300 / (total - 1));
  const fade = 0.35 + 0.65 * Math.sin((index / (total - 1)) * Math.PI);
  const ring = document.createElement('div');
  ring.className = 'scan-ring';
  ring.style.width = diameter + 'px';
  ring.style.height = diameter + 'px';
  ring.style.marginLeft = (-diameter / 2) + 'px';
  ring.style.marginTop = (-diameter / 2) + 'px';
  ring.style.opacity = fade;
  ring.style.transform = `translateY(${y}px) rotateX(90deg)`;
  return ring;
}

function buildScan() {
  const spin = document.createElement('div');
  spin.className = 'scan-spin';
  SCAN_PROFILE.forEach((d, i) => spin.appendChild(buildScanRing(d, i, SCAN_PROFILE.length)));

  const pulse = document.createElement('div');
  pulse.className = 'scan-pulse';
  spin.appendChild(pulse);

  const axis = document.createElement('div');
  axis.className = 'scan-axis';
  spin.appendChild(axis);

  const tilt = document.createElement('div');
  tilt.className = 'scan-tilt';
  tilt.appendChild(spin);

  const sweep = document.createElement('div');
  sweep.className = 'scan-sweep';

  const root = document.createElement('div');
  root.className = 'scan-root';
  root.appendChild(tilt);
  root.appendChild(sweep);
  return root;
}

function mountScan3D(containerId) {
  const container = document.getElementById(containerId);
  if (!container || container.dataset.scanMounted) return;
  container.appendChild(buildScan());
  container.dataset.scanMounted = '1';
}

document.addEventListener('DOMContentLoaded', () => mountScan3D('auth-scan'));
