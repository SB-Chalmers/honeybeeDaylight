import os
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.tri as tri

RESULTS_DIR = './annual_daylight_sim/annual_daylight'
GRID_NAME = 'floor_grid'

pts_path = os.path.join(RESULTS_DIR, 'model/grid', f'{GRID_NAME}.pts')
da_path = os.path.join(RESULTS_DIR, 'metrics/da', f'{GRID_NAME}.da')

pts = np.loadtxt(pts_path)
da = np.loadtxt(da_path)

x = pts[:, 0]
y = pts[:, 1]
triang = tri.Triangulation(x, y)

sda_mask = da >= 50.0
sda_value = float(np.mean(sda_mask) * 100.0)

fig, axes = plt.subplots(1, 2, figsize=(12, 5), constrained_layout=True)

# Left: DA heatmap
ax = axes[0]
tcf = ax.tricontourf(triang, da, levels=20, cmap='YlOrRd')
fig.colorbar(tcf, ax=ax, label='Daylight Autonomy (DA %)')
ax.scatter(x, y, c=da, cmap='YlOrRd', s=14, edgecolors='k', linewidths=0.25)
ax.set_title('Annual Daylight: DA Heatmap')
ax.set_xlabel('X (m) -> East')
ax.set_ylabel('Y (m) -> North')
ax.set_aspect('equal')

# Right: sDA pass/fail map
ax2 = axes[1]
pass_color = np.where(sda_mask, 1, 0)
ax2.scatter(x[sda_mask], y[sda_mask], c='#1a9850', s=28, label='Pass (DA >= 50%)')
ax2.scatter(x[~sda_mask], y[~sda_mask], c='#d73027', s=28, label='Fail (DA < 50%)')
ax2.set_title(f'sDA300/50 = {sda_value:.1f}% ({int(sda_mask.sum())}/{len(sda_mask)} sensors)')
ax2.set_xlabel('X (m) -> East')
ax2.set_ylabel('Y (m) -> North')
ax2.set_aspect('equal')
ax2.legend(loc='best', frameon=True)

out_path = os.path.join(RESULTS_DIR, f'{GRID_NAME}_annual_daylight.png')
fig.savefig(out_path, dpi=150)
plt.show()

print(f'Saved: {out_path}')
print(f'sDA300/50: {sda_value:.2f}%')
