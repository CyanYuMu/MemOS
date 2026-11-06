/** mock 后端的内存数据集与落盘。
 *
 *  所有资源都是同一张表的行，加一个资源 = 在 data.ts 里加一个种子数组，
 *  不用在路由里补分支。
 *
 *  落盘文件带 schema 版本号：字段名改了之后，老文件直接作废重新生成，
 *  免得读出上一版的旧字段把出参搅乱。
 */

import fs from 'node:fs'
import path from 'node:path'
import { COLLECTIONS, SCHEMA_VERSION, type Db } from './data.ts'

export type { Db }

const isRowArray = (v: unknown): v is { id: string; [k: string]: unknown }[] => Array.isArray(v)

export function load(file: string): Db {
  try {
    const raw = JSON.parse(fs.readFileSync(file, 'utf8')) as { __v?: number } & Partial<Db>
    if (raw.__v !== SCHEMA_VERSION) throw new Error(`落盘文件是 v${raw.__v ?? '?'}，当前 v${SCHEMA_VERSION}`)
    // 数据集里新增的表，老落盘文件里没有，按种子补上
    const merged: Db = { ...COLLECTIONS }
    for (const k of Object.keys(COLLECTIONS) as (keyof Db)[]) {
      const saved = raw[k]
      if (isRowArray(saved) && saved.length) merged[k] = saved
    }
    return merged
  } catch {
    const db: Db = { ...COLLECTIONS }
    save(file, db)
    return db
  }
}

export function save(file: string, db: Db) {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, JSON.stringify({ __v: SCHEMA_VERSION, ...db }, null, 1))
}
