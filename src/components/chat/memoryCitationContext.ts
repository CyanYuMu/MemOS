import { createContext } from 'react'
import type { MemoryCitationScope } from './memoryCitations'
export const MemoryCitationContext = createContext<MemoryCitationScope | undefined>(undefined)
