import { createHash } from 'node:crypto'
import { access, cp, lstat, mkdir, readFile, readdir, rename, rm } from 'node:fs/promises'
import { join, relative } from 'node:path'
import type { ResolvedConfig } from '../config.js'

/** uv 识别项目根所需的两项，也是副本是否完整的判据。 */
const REQUIRED = [['pyproject.toml'], ['connector', 'cli.py']] as const
/**
 * 被取代的镜像只是占地方。三个 Desktop 通道共享同一个 stateRoot，另一通道可能正在
 * 用上一份副本跑 uv，所以回收要留出足够长的时间，只清理确定过期的目录。
 */
const STALE_MIRROR_MS = 24 * 60 * 60_000

async function complete(directory: string): Promise<boolean> {
  try {
    for (const parts of REQUIRED) await access(join(directory, ...parts))
    return true
  } catch {
    return false
  }
}

async function listFiles(root: string, current = root): Promise<string[]> {
  const found: string[] = []
  for (const entry of (await readdir(current, { withFileTypes: true })).sort((left, right) => left.name.localeCompare(right.name))) {
    const path = join(current, entry.name)
    if (entry.isDirectory()) found.push(...await listFiles(root, path))
    else if (entry.isFile()) found.push(path)
  }
  return found
}

/** 负载内容寻址：内容变了才重新镜像，同一份负载已解析出的 uv.lock 得以跨启动复用。 */
async function payloadDigest(source: string): Promise<string> {
  const hash = createHash('sha256')
  for (const file of await listFiles(source)) {
    hash.update(relative(source, file)).update('\0').update(await readFile(file)).update('\0')
  }
  return hash.digest('hex').slice(0, 16)
}

/**
 * 把打包内的 Connector 项目镜像到插件自己的数据目录，返回可写副本的路径。
 *
 * uv 在项目目录里写 uv.lock，而插件包目录不能当项目目录用：asar 归档不可写，共享
 * 安装的 /Applications 对普通用户不可写，在已签名的 macOS 包里落文件还会直接废掉
 * code signature。副本先落到同级的临时目录再 rename 就位，因此一次启动要么复用一份
 * 完整副本，要么发布一份完整副本；uv.lock 随后就留在这份副本里。
 */
export async function materializeConnectorProject(config: ResolvedConfig): Promise<string> {
  const source = config.connectorSourceDir
  if (!await complete(source)) throw new Error('未找到内部 Connector 源码，请重新构建插件或配置 connectorSourceDir。')
  const root = join(config.stateRoot, 'connector-source')
  const target = join(root, await payloadDigest(source))
  if (!await complete(target)) {
    await mkdir(root, { recursive: true, mode: 0o700 })
    const staging = `${target}.partial-${process.pid}`
    await rm(staging, { recursive: true, force: true })
    await cp(source, staging, { recursive: true, dereference: true })
    if (!await complete(staging)) throw new Error('内部 Connector 源码不完整。')
    await rm(target, { recursive: true, force: true })
    await rename(staging, target)
  }
  await prune(root, target)
  return target
}

async function prune(root: string, target: string): Promise<void> {
  const keep = target.slice(root.length + 1)
  const now = Date.now()
  for (const entry of await readdir(root)) {
    if (entry === keep) continue
    const path = join(root, entry)
    const stat = await lstat(path).catch(() => undefined)
    if (stat === undefined || now - stat.mtimeMs < STALE_MIRROR_MS) continue
    // 正在被另一通道使用的目录会因 rename/删除竞争而失败，回收不能影响本次启动。
    await rm(path, { recursive: true, force: true }).catch(() => undefined)
  }
}
