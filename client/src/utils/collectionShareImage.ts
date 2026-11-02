export interface ShareStats {
  owned: number;
  target: number;
  percentage: number;
  completeSets: number;
  totalSets: number;
  rainbowOwned: number;
  rainbowTotal: number;
}

/** Draws from counts only, so the picture can never carry a UID. */
export async function collectionShareImage(stats: ShareStats): Promise<Blob> {
  const canvas = document.createElement('canvas');
  canvas.width = 960;
  canvas.height = 480;
  const context = canvas.getContext('2d');
  if (!context) throw new Error('无法生成图片');

  const background = context.createLinearGradient(0, 0, 960, 480);
  background.addColorStop(0, '#fff1f5');
  background.addColorStop(1, '#eef2ff');
  context.fillStyle = background;
  context.fillRect(0, 0, 960, 480);

  const font = '"Microsoft YaHei", "PingFang SC", sans-serif';
  context.fillStyle = '#6b5563';
  context.font = `28px ${font}`;
  context.fillText('祝福闪光收集进度', 64, 88);

  context.fillStyle = '#3f2f3a';
  context.font = `bold 96px ${font}`;
  context.fillText(`${stats.percentage}%`, 60, 200);
  context.fillStyle = '#6b5563';
  context.font = `28px ${font}`;
  context.fillText(`${stats.owned} / ${stats.target} 个颜色`, 64, 248);

  context.fillStyle = '#ffffff';
  context.beginPath();
  context.roundRect(64, 276, 832, 20, 10);
  context.fill();
  context.fillStyle = '#f28fa8';
  context.beginPath();
  context.roundRect(64, 276, Math.max(20, 832 * Math.min(stats.percentage, 100) / 100), 20, 10);
  context.fill();

  const columns = [
    ['收齐套组', `${stats.completeSets} / ${stats.totalSets}`],
    ['彩色', `${stats.rainbowOwned} / ${stats.rainbowTotal}`],
  ];
  columns.forEach(([label, value], index) => {
    const x = 64 + index * 280;
    context.fillStyle = '#6b5563';
    context.font = `22px ${font}`;
    context.fillText(label, x, 346);
    context.fillStyle = '#3f2f3a';
    context.font = `bold 36px ${font}`;
    context.fillText(value, x, 392);
  });

  context.fillStyle = '#8b7a85';
  context.font = `20px ${font}`;
  context.fillText('MaaNikki · maanikki.cn', 64, 444);

  return new Promise((resolve, reject) => canvas.toBlob(
    (blob) => blob ? resolve(blob) : reject(new Error('无法生成图片')), 'image/png'));
}
