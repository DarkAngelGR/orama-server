import { cpSync } from 'node:fs';
for (const dir of ['views', 'public']) cpSync(dir, `dist/${dir}`, { recursive: true });
