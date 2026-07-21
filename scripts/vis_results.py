import os
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.tri as tri

RESULTS_DIR = './daylight_factor_sim/daylight_factor'
GRID_NAME = 'floor_grid'

# Load sensor positions (x y z nx ny nz)
pts_path = os.path.join(RESULTS_DIR, 'model/grid', f'{GRID_NAME}.pts')
pts = np.loadtxt(pts_path)
x, y = pts[:, 0], pts[:, 1]

# Load daylight factor results (one value per sensor, in %)
res_path = os.path.join(RESULTS_DIR, 'results', f'{GRID_NAME}.res')
df_values = np.loadtxt(res_path)

# Triangulate for smooth interpolated heatmap
triang = tri.Triangulation(x, y)

fig, ax = plt.subplots(figsize=(7, 6))
tcf = ax.tricontourf(triang, df_values, levels=20, cmap='YlOrRd')
cbar = fig.colorbar(tcf, ax=ax)
cbar.set_label('Daylight Factor (%)')

ax.set_title(f'Daylight Factor — {GRID_NAME}')
ax.set_xlabel('X (m)  →  East')
ax.set_ylabel('Y (m)  →  North')
ax.set_aspect('equal')

# North arrow
ax.annotate('N', xy=(0.97, 0.97), xycoords='axes fraction',
            fontsize=13, fontweight='bold', ha='right', va='top')
ax.annotate('', xy=(0.97, 0.97), xytext=(0.97, 0.88), xycoords='axes fraction',
            arrowprops=dict(arrowstyle='->', color='black', lw=1.5))

# Overlay sensor points
ax.scatter(x, y, c=df_values, cmap='YlOrRd', s=12, edgecolors='k', linewidths=0.3, zorder=5)

out_path = os.path.join(RESULTS_DIR, f'{GRID_NAME}_df.png')
plt.tight_layout()
plt.savefig(out_path, dpi=150)
plt.show()
print(f"Saved: {out_path}")
