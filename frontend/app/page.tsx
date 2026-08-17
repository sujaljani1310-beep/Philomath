"use client"

import { useState, useRef, useEffect, useCallback } from "react"
import type { MouseEvent } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet"
import { Badge } from "@/components/ui/badge"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import {
  Plus,
  ChevronDown,
  Send,
  Mic,
  Settings,
  Menu,
  MessageSquare,
  Zap,
  User,
  Bot,
  Brain,
  Sparkles,
  Cpu,
  Network,
  X,
  Trash2,
} from "lucide-react"
import { cn } from "@/lib/utils"

type Mode = "basic" | "manual" | "auto"

type Message = {
  id: string
  content: string
  sender: "user" | "ai"
  timestamp: Date
  aiModel?: string
  provider_used?: string
  model_used?: string
  search_grounding?: string | boolean
  routing?: {
    confidence_level?: string
    confidence_value?: number
    category?: string
    reason?: string
    hard_requirements_unmet?: boolean
  }
}

type Conversation = {
  id: string
  title: string
  preview: string
  messages: Message[]
  lastUpdated: Date
}

type AIProvider = {
  id: string
  name: string
  description: string
  icon: any
  color: string
}

const modes = [
  {
    id: "basic" as Mode,
    label: "Basic Mode",
    description: "Simple conversation & quick research",
    icon: MessageSquare,
  },
  {
    id: "manual" as Mode,
    label: "Manual Mode",
    description: "Select AI provider",
    icon: User,
  },
  {
    id: "auto" as Mode,
    label: "Auto Mode",
    description: "System auto-selects best AI for the task",
    icon: Zap,
  },
]


const aiProviders: AIProvider[] = [
  {
    id: "openrouter",
    name: "OpenRouter",
    description: "Access multiple AI models through one API",
    icon: Network,
    color: "bg-emerald-500",
  },
  {
    id: "cerebras",
    name: "Cerebras",
    description: "Ultra-fast inference with custom hardware",
    icon: Brain,
    color: "bg-orange-500",
  },
  {
    id: "nvidia",
    name: "NVIDIA",
    description: "GPU-accelerated AI models",
    icon: Cpu,
    color: "bg-green-500",
  },
  {
    id: "gemini",
    name: "Gemini",
    description: "Google's multimodal AI",
    icon: Sparkles,
    color: "bg-blue-500",
  },
  {
    id: "grok",
    name: "Grok",
    description: "xAI's conversational AI",
    icon: X,
    color: "bg-gray-500",
  },
]

const DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"

export default function AGIInterface() {
  const [selectedMode, setSelectedMode] = useState<Mode>("basic")
  const [inputValue, setInputValue] = useState("")
  const [selectedProvider, setSelectedProvider] = useState<string | null>("cerebras")
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarVisible, setSidebarVisible] = useState(false)
  const [isLoading, setIsLoading] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [backendUrl, setBackendUrl] = useState(DEFAULT_BACKEND_URL)
  const [toastMessage, setToastMessage] = useState<string | null>(null)
  const [selectedFiles, setSelectedFiles] = useState<File[]>([])

  const fileInputRef = useRef<HTMLInputElement>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  const currentMode = modes.find((m) => m.id === selectedMode)
  const activeConversation = conversations.find((c) => c.id === activeConversationId)
  const currentMessages = activeConversation?.messages || []

  const showToast = useCallback((message: string) => {
    setToastMessage(message)
    setTimeout(() => setToastMessage(null), 3000)
  }, [])

  const loadConversations = useCallback(async () => {
    try {
      const response = await fetch(`${backendUrl}/api/conversations`)

      if (!response.ok) return

      const data = await response.json()

      if (data.conversations && Array.isArray(data.conversations)) {
        const formattedConversations: Conversation[] = data.conversations.map((conv: any) => ({
          id: conv.id,
          title: conv.title || "Untitled Chat",
          preview: conv.preview || "Saved conversation",
          messages: [],
          lastUpdated: new Date(conv.updated_at || conv.created_at || Date.now()),
        }))

        setConversations((prev) => {
          const localEmptyChats = prev.filter((conv) => conv.messages.length === 0 && conv.title === "New Chat")
          return [...localEmptyChats, ...formattedConversations]
        })
      }
    } catch (error) {
      console.log("[Philomath] Could not load conversations from backend:", error)
    }
  }, [backendUrl])

  const loadConversationMessages = useCallback(
    async (conversationId: string) => {
      try {
        const response = await fetch(`${backendUrl}/api/conversations/${conversationId}`)

        if (!response.ok) return

        const data = await response.json()

        if (data.messages && Array.isArray(data.messages)) {
          const formattedMessages: Message[] = data.messages.map((msg: any, index: number) => ({
            id: msg.id || `${conversationId}_${index}_${Date.now()}`,
            content: msg.content,
            sender: msg.role === "user" ? "user" : "ai",
            timestamp: new Date(msg.created_at || Date.now()),
            aiModel: msg.model_used,
            provider_used: msg.provider_used,
            model_used: msg.model_used,
            search_grounding: msg.search_grounding,
          }))

          setConversations((prev) =>
            prev.map((conv) =>
              conv.id === conversationId
                ? {
                    ...conv,
                    messages: formattedMessages,
                  }
                : conv,
            ),
          )
        }
      } catch (error) {
        console.log("[Philomath] Could not load conversation messages:", error)
      }
    },
    [backendUrl],
  )

  useEffect(() => {
    loadConversations()
  }, [loadConversations])

  useEffect(() => {
    if (!activeConversationId) return

    const conv = conversations.find((c) => c.id === activeConversationId)

    if (conv && conv.messages.length === 0) {
      loadConversationMessages(activeConversationId)
    }
  }, [activeConversationId, conversations, loadConversationMessages])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [currentMessages.length, isLoading])

  const handleSendMessage = async () => {
    if (!inputValue.trim() || isLoading) return

    if (selectedMode === "manual" && !selectedProvider) {
      showToast("Please select a provider first.")
      return
    }

    const userMessage = inputValue.trim()
    setInputValue("")
    setIsLoading(true)

    let conversationId = activeConversationId
    let fileContext: string | null = null
    let hasFiles = false

    try {
      if (selectedMode !== "basic" && selectedFiles.length > 0) {
        const formData = new FormData()

        selectedFiles.forEach((file) => {
          formData.append("files", file)
        })

        const uploadResponse = await fetch(`${backendUrl}/api/files/upload`, {
          method: "POST",
          body: formData,
        })

        if (!uploadResponse.ok) {
          throw new Error("File upload failed")
        }

        const uploadData = await uploadResponse.json()

        if (!uploadData.success) {
          throw new Error("Backend did not accept the files")
        }

        const extractedFileText =
          typeof uploadData.combined_text === "string"
            ? uploadData.combined_text.trim()
            : ""

        if (!extractedFileText) {
          throw new Error("Philomath could not extract readable text from the uploaded file(s)")
        }

        fileContext = extractedFileText
        hasFiles = true

        showToast(
          `${uploadData.count} file${uploadData.count === 1 ? "" : "s"} uploaded and read`
        )
      }

    const newUserMessage: Message = {
      id: Date.now().toString(),
      content: userMessage,
      sender: "user",
      timestamp: new Date(),
    }

    if (!conversationId) {
      conversationId = `chat_${Date.now()}`

      const newConversation: Conversation = {
        id: conversationId,
        title: userMessage.slice(0, 30) + (userMessage.length > 30 ? "..." : ""),
        preview: userMessage.slice(0, 50),
        messages: [newUserMessage],
        lastUpdated: new Date(),
      }

      setConversations((prev) => [newConversation, ...prev])
      setActiveConversationId(conversationId)
    } else {
      const existingConv = conversations.find((c) => c.id === conversationId)
      const isFirstMessage = existingConv && existingConv.messages.length === 0

      setConversations((prev) =>
        prev.map((conv) =>
          conv.id === conversationId
            ? {
                ...conv,
                messages: [...conv.messages, newUserMessage],
                lastUpdated: new Date(),
                title: isFirstMessage ? userMessage.slice(0, 30) + (userMessage.length > 30 ? "..." : "") : conv.title,
                preview: isFirstMessage ? userMessage.slice(0, 50) : userMessage.slice(0, 50),
              }
            : conv,
        ),
      )
    }

      const payload: any = {
        message: userMessage,
        mode: selectedMode,
        conversation_id: conversationId,
        has_files: hasFiles,
      }

      if (hasFiles && fileContext) {
        payload.file_context = fileContext
      }

      if (selectedMode === "manual" && selectedProvider) {
        payload.provider = selectedProvider
      }

      const response = await fetch(`${backendUrl}/api/chat/send`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      })

      if (!response.ok) {
        throw new Error("Failed to get response from backend")
      }

      const data = await response.json()

      const aiResponse: Message = {
        id: (Date.now() + 1).toString(),
        content: data.answer || "I received your message but couldn't generate a response.",
        sender: "ai",
        timestamp: new Date(),
        aiModel: data.model_used,
        provider_used: data.provider_used,
        model_used: data.model_used,
        search_grounding: data.search_grounding,
        routing: data.routing,
      }

      setConversations((prev) =>
        prev.map((conv) =>
          conv.id === conversationId
            ? {
                ...conv,
                messages: [...conv.messages, aiResponse],
                lastUpdated: new Date(),
              }
            : conv,
        ),
      )

      if (selectedFiles.length > 0) {
        setSelectedFiles([])

        if (fileInputRef.current) {
          fileInputRef.current.value = ""
        }
      }
    } catch (error) {
      console.log("[Philomath] Error sending message:", error)

      const errorMessage: Message = {
        id: (Date.now() + 1).toString(),
        content: `Sorry, I couldn't connect to the backend. Please check if the server is running at ${backendUrl}`,
        sender: "ai",
        timestamp: new Date(),
      }

      setConversations((prev) =>
        prev.map((conv) =>
          conv.id === conversationId
            ? {
                ...conv,
                messages: [...conv.messages, errorMessage],
                lastUpdated: new Date(),
              }
            : conv,
        ),
      )
    } finally {
      setIsLoading(false)
    }
  }

  const handleNewChat = () => {
    if (activeConversationId) {
      const currentConv = conversations.find((c) => c.id === activeConversationId)

      if (currentConv && currentConv.messages.length === 0) {
        setSidebarOpen(false)
        return
      }
    }

    const newConversationId = `chat_${Date.now()}`

    const newConversation: Conversation = {
      id: newConversationId,
      title: "New Chat",
      preview: "Start a new conversation...",
      messages: [],
      lastUpdated: new Date(),
    }

    setConversations((prev) => [newConversation, ...prev])
    setActiveConversationId(newConversationId)
    setSidebarOpen(false)
  }

 const handleDeleteChat = async (e: MouseEvent<HTMLButtonElement>, conversationId: string) => {
  e.stopPropagation()

  // Remove from UI first so it feels instant
  setConversations((prev) => {
    const filtered = prev.filter((c) => c.id !== conversationId)

    if (activeConversationId === conversationId) {
      if (filtered.length > 0) {
        setActiveConversationId(filtered[0].id)
      } else {
        const newChatId = `chat_${Date.now()}`

        const newChat: Conversation = {
          id: newChatId,
          title: "New Chat",
          preview: "Start a new conversation...",
          messages: [],
          lastUpdated: new Date(),
        }

        setActiveConversationId(newChatId)
        return [newChat]
      }
    }

    return filtered
  })

  try {
    const response = await fetch(`${backendUrl}/api/conversations/${conversationId}`, {
      method: "DELETE",
    })

    if (!response.ok) {
      throw new Error("Delete request failed")
    }
  } catch (error) {
    console.log("[Philomath] Could not delete conversation from backend:", error)
    showToast("Could not delete this saved chat from Supabase.")
    loadConversations()
  }
}

  const handleFileUpload = () => {
    fileInputRef.current?.click()
  }

  const removeSelectedFile = (indexToRemove: number) => {
    setSelectedFiles((prev) =>
      prev.filter((_, index) => index !== indexToRemove)
    )

    if (fileInputRef.current) {
      fileInputRef.current.value = ""
    }
  }

  const handleModeChange = (mode: Mode) => {
    setSelectedMode(mode)

    if (mode === "basic") {
      setSelectedFiles([])

      if (fileInputRef.current) {
        fileInputRef.current.value = ""
      }
    }
  }

  const handleVoiceInput = () => {
    showToast("Voice input is coming soon.")
  }

  const selectProvider = (providerId: string) => {
    setSelectedProvider(providerId)
  }

  const SidebarContent = () => (
    <div className="flex h-full flex-col border-r border-gray-700/50 bg-gray-900/80 backdrop-blur-md">
      <div className="border-b border-gray-700/50 p-4">
        <Button onClick={handleNewChat} className="w-full bg-blue-600 text-white hover:bg-blue-700">
          <Plus className="mr-2 h-4 w-4" />
          New Chat
        </Button>
      </div>

      <ScrollArea className="flex-1 p-4">
        <div className="space-y-2">
          {conversations.map((conversation) => (
            <div
              key={conversation.id}
              onClick={() => {
                setActiveConversationId(conversation.id)
                setSidebarOpen(false)
              }}
              className={cn(
                "group relative w-full cursor-pointer rounded-lg border p-3 text-left transition-all",
                "hover:bg-gray-800/50",
                activeConversationId === conversation.id
                  ? "border-blue-500/50 bg-blue-600/20 shadow-[0_0_15px_rgba(59,130,246,0.3)]"
                  : "border-transparent bg-gray-800/30",
              )}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium text-white">{conversation.title}</div>
                  <div className="mt-1 truncate text-xs text-gray-400">{conversation.preview}</div>
                </div>

                <button
                  onClick={(e) => handleDeleteChat(e, conversation.id)}
                  className="flex-shrink-0 rounded-md p-1.5 text-gray-400 opacity-0 transition-all hover:bg-red-500/20 hover:text-red-400 group-hover:opacity-100"
                  title="Delete chat"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            </div>
          ))}
        </div>
      </ScrollArea>
    </div>
  )

  const renderMessages = () => (
    <div className="mx-auto w-full max-w-4xl flex-1 overflow-y-auto px-4">
      <div className="space-y-6 pb-20 pt-4">
        {currentMessages.map((message) => (
          <div key={message.id} className={cn("flex gap-4", message.sender === "user" ? "justify-end" : "justify-start")}>
            {message.sender === "ai" && (
              <div className="flex flex-col items-center">
                <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-blue-600">
                  <Bot className="h-4 w-4" />
                </div>
              </div>
            )}

            <div
              className={cn(
                "max-w-2xl rounded-2xl p-4",
                message.sender === "user" ? "ml-12 bg-blue-600 text-white" : "bg-gray-800/50 text-gray-100",
              )}
            >
              <p className="whitespace-pre-wrap leading-relaxed">{message.content}</p>

              {message.sender === "ai" &&
                (message.provider_used || message.model_used || message.search_grounding !== undefined) && (
                  <div className="mt-3 flex flex-wrap gap-2 border-t border-gray-700/50 pt-3 text-xs text-gray-400">
                    {message.provider_used && (
                      <Badge variant="outline" className="bg-gray-700/50 text-xs border-gray-600">
                        Provider: {message.provider_used}
                      </Badge>
                    )}

                    {message.model_used && (
                      <Badge variant="outline" className="bg-gray-700/50 text-xs border-gray-600">
                        Model: {message.model_used}
                      </Badge>
                    )}

                    {message.routing?.category && (
  <Badge
    variant="outline"
    className="border-blue-500/50 bg-blue-500/10 text-xs text-blue-300"
  >
    {message.routing.category}
  </Badge>
)}

{message.routing?.confidence_level && (
  <Badge
    variant="outline"
    className="border-purple-500/50 bg-purple-500/10 text-xs text-purple-300"
  >
    {message.routing.confidence_level} confidence
  </Badge>
)}
                  </div>
                )}
            </div>

            {message.sender === "user" && (
              <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-gray-600">
                <User className="h-4 w-4" />
              </div>
            )}
          </div>
        ))}

        {isLoading && (
          <div className="flex justify-start gap-4">
            <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-blue-600">
              <Bot className="h-4 w-4" />
            </div>

            <div className="rounded-2xl bg-gray-800/50 p-4 text-gray-100">
              <div className="flex items-center gap-2">
                <div className="h-2 w-2 animate-pulse rounded-full bg-blue-400" />
                <div className="h-2 w-2 animate-pulse rounded-full bg-blue-400" style={{ animationDelay: "0.2s" }} />
                <div className="h-2 w-2 animate-pulse rounded-full bg-blue-400" style={{ animationDelay: "0.4s" }} />
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>
    </div>
  )

  const renderManualModePanel = () => {
    if (selectedMode !== "manual") return null

    return (
      <div className="mx-auto w-full max-w-3xl px-4 pb-4">
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-gray-700/50 bg-gray-800/30 p-4">
          <span className="text-sm font-medium text-gray-400">Select Provider:</span>

          {aiProviders.map((provider) => (
            <button
              key={provider.id}
              onClick={() => selectProvider(provider.id)}
              className={cn(
                "flex items-center gap-2 rounded-lg border px-3 py-2 transition-all hover:scale-105",
                selectedProvider === provider.id
                  ? "border-blue-500 bg-blue-500/20 text-blue-300 shadow-lg shadow-blue-500/20"
                  : "border-gray-600 bg-gray-800/50 text-gray-300 hover:border-gray-500",
              )}
              title={`${provider.name} - ${provider.description}`}
            >
              <div className={cn("flex h-6 w-6 items-center justify-center rounded-full", provider.color)}>
                <provider.icon className="h-3 w-3 text-white" />
              </div>

              <span className="text-xs font-medium">{provider.name}</span>

              {selectedProvider === provider.id && <div className="h-2 w-2 rounded-full bg-blue-400" />}
            </button>
          ))}
        </div>

        {selectedProvider && (
          <div className="mt-3 rounded-lg border border-blue-500/30 bg-blue-500/10 p-3">
            <div className="flex items-center gap-2">
              <Zap className="h-4 w-4 text-blue-400" />
              <span className="text-sm text-blue-400">
                {aiProviders.find((p) => p.id === selectedProvider)?.name} selected
              </span>
            </div>
          </div>
        )}
      </div>
    )
  }

  return (
    <div
      className={cn(
        "min-h-screen overflow-x-hidden text-white relative transition-all duration-1000",
        selectedMode === "auto"
          ? "bg-gradient-to-br from-slate-900 via-blue-950/50 to-purple-950/30"
          : "bg-gradient-to-br from-slate-900 via-blue-950/30 to-black",
      )}
    >
      <div
        className={cn(
          "pointer-events-none absolute inset-0 transition-opacity duration-1000",
          selectedMode === "auto"
            ? "bg-[radial-gradient(circle_at_50%_50%,rgba(59,130,246,0.2),rgba(147,51,234,0.1),transparent_70%)] opacity-100"
            : "bg-[radial-gradient(circle_at_50%_50%,rgba(59,130,246,0.1),transparent_50%)] opacity-100",
        )}
      />

      {toastMessage && (
        <div className="fixed right-4 top-4 z-50 rounded-lg border border-gray-700 bg-gray-800 px-4 py-3 text-white shadow-lg">
          {toastMessage}
        </div>
      )}

      <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
        <DialogContent className="border-gray-700 bg-gray-900 text-white">
          <DialogHeader>
            <DialogTitle>Settings</DialogTitle>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <div>
              <label className="mb-2 block text-sm text-gray-400">Theme</label>
              <div className="rounded-lg border border-gray-700 bg-gray-800 px-4 py-2 text-gray-300">
                Dark mode
              </div>
            </div>

            <div>
              <label className="mb-2 block text-sm text-gray-400">Backend URL</label>
              <Input
                value={backendUrl}
                onChange={(e) => setBackendUrl(e.target.value)}
                className="border-gray-700 bg-gray-800 text-white"
                placeholder="http://127.0.0.1:8000"
              />
            </div>

            <div>
              <label className="mb-2 block text-sm text-gray-400">About</label>
              <div className="rounded-lg border border-gray-700 bg-gray-800 px-4 py-2 text-gray-300">
                Philomath AI assistant prototype
              </div>
            </div>
          </div>

          <div className="flex justify-end">
            <Button onClick={() => setSettingsOpen(false)} className="bg-blue-600 hover:bg-blue-700">
              Close
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <div className="relative flex h-screen overflow-hidden">
        <div
          className={cn(
            "fixed left-0 top-0 z-30 hidden h-full transition-all duration-300 ease-out md:block",
            sidebarVisible ? "w-80" : "w-0 overflow-hidden",
          )}
        >
          {sidebarVisible && <SidebarContent />}
        </div>

        <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
          <SheetContent side="left" className="w-80 bg-gray-900/95 p-0 backdrop-blur-sm">
            <SidebarContent />
          </SheetContent>
        </Sheet>

        <div className={cn("flex flex-1 flex-col transition-all duration-300 ease-out", sidebarVisible ? "md:ml-80" : "ml-0")}>
          <div className="flex flex-shrink-0 items-center justify-between border-b border-gray-700/50 bg-gray-900/30 p-4 backdrop-blur-md">
            <div className="hidden md:block">
              <Button
                onClick={() => setSidebarVisible(!sidebarVisible)}
                variant="outline"
                size="sm"
                className={cn(
                  "border-blue-500/50 bg-transparent text-blue-400 transition-all duration-200 hover:bg-blue-500/20",
                  !sidebarVisible && "shadow-lg shadow-blue-500/20 ring-2 ring-blue-500/30",
                )}
              >
                <Menu className="mr-2 h-4 w-4" />
                {sidebarVisible ? "Hide Chats" : "Show Chats"}
              </Button>
            </div>

            <div className="md:hidden">
              <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
                <SheetTrigger asChild>
                  <Button variant="outline" size="sm" className="border-blue-500/50 bg-transparent text-blue-400 hover:bg-blue-500/20">
                    <Menu className="mr-2 h-4 w-4" />
                    Chats
                  </Button>
                </SheetTrigger>
              </Sheet>
            </div>

            <div className="flex items-center gap-2">
              <Badge
                variant="outline"
                className={cn(
                  "text-xs",
                  selectedMode === "auto" && "border-blue-500 bg-blue-500/10 text-blue-400",
                  selectedMode === "manual" && "border-orange-500 bg-orange-500/10 text-orange-400",
                  selectedMode === "basic" && "border-gray-500 bg-gray-500/10 text-gray-400",
                )}
              >
                {currentMode?.label}
              </Badge>

              <Button
                onClick={() => setSettingsOpen(true)}
                variant="ghost"
                size="icon"
                className="text-gray-400 transition-colors hover:text-blue-400"
              >
                <Settings className="h-5 w-5" />
              </Button>
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col">
            <div className="flex-shrink-0 pt-10 pb-5 text-center md:pt-14 md:pb-7">
              <h1 className="mb-8 bg-gradient-to-r from-white via-blue-200 to-blue-400 bg-clip-text text-4xl font-bold tracking-wide text-transparent drop-shadow-2xl transition-all duration-1000 lg:text-6xl">
                Philomath
              </h1>

              <div className="mx-auto max-w-3xl px-4">
                <div
                  className={cn(
                    "flex items-center rounded-2xl border bg-gray-800/60 backdrop-blur-md transition-all duration-300",
                    selectedMode === "auto"
                      ? "border-blue-500/30 shadow-lg shadow-blue-500/20 focus-within:border-blue-400/90 focus-within:ring-2 focus-within:ring-blue-400/50"
                      : "border-gray-600/50 focus-within:border-blue-500/70 focus-within:ring-2 focus-within:ring-blue-500/30",
                  )}
                >
                  {selectedMode !== "basic" && (
                    <Button
                      onClick={handleFileUpload}
                      variant="ghost"
                      size="sm"
                      className={cn(
                        "ml-3 text-gray-400 transition-all duration-200 hover:bg-blue-500/10 hover:text-blue-400",
                        selectedMode === "auto" && "text-blue-300 hover:text-blue-200",
                      )}
                    >
                      <Plus className="mr-2 h-4 w-4" />
                      Add Files
                    </Button>
                  )}

                  <Input
                    value={inputValue}
                    onChange={(e) => setInputValue(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault()
                        handleSendMessage()
                      }
                    }}
                    placeholder={
                      selectedMode === "auto"
                        ? "Ask anything - AI will auto-select the best model..."
                        : selectedMode === "manual"
                          ? "Select a provider above, then ask your question..."
                          : "Ask me anything..."
                    }
                    className="flex-1 border-none bg-transparent px-4 py-4 text-lg text-white placeholder-gray-400 focus:ring-0"
                    disabled={isLoading}
                  />

                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="ghost" size="sm" className="mr-3 text-gray-400 hover:bg-blue-500/10 hover:text-blue-400">
                        {currentMode?.icon && <currentMode.icon className="mr-3 mt-0.5 h-4 w-4 flex-shrink-0" />}
                        Mode
                        <ChevronDown className="ml-2 h-4 w-4" />
                      </Button>
                    </DropdownMenuTrigger>

                    <DropdownMenuContent align="end" className="w-64 border-gray-700 bg-gray-800/95 backdrop-blur-sm">
                      {modes.map((mode) => (
                        <DropdownMenuItem
                          key={mode.id}
                          onClick={() => handleModeChange(mode.id)}
                          className={cn(
                            "flex cursor-pointer items-start p-3",
                            selectedMode === mode.id ? "bg-blue-600/20 text-blue-300" : "text-gray-300 hover:bg-gray-700/50",
                          )}
                        >
                          <mode.icon className="mr-3 mt-0.5 h-4 w-4 flex-shrink-0" />
                          <div>
                            <div className="font-medium">{mode.label}</div>
                            <div className="mt-1 text-xs text-gray-400">{mode.description}</div>
                          </div>
                        </DropdownMenuItem>
                      ))}
                    </DropdownMenuContent>
                  </DropdownMenu>

                  <Button
                    onClick={handleSendMessage}
                    disabled={!inputValue.trim() || isLoading}
                    size="sm"
                    className="ml-2 bg-blue-600 transition-all duration-200 hover:bg-blue-700 hover:shadow-lg hover:shadow-blue-500/20 disabled:bg-gray-700 disabled:text-gray-400"
                  >
                    <Send className="h-4 w-4" />
                  </Button>

                  <Button
                    onClick={handleVoiceInput}
                    variant="ghost"
                    size="sm"
                    className="ml-2 mr-3 text-gray-400 transition-all duration-200 hover:bg-blue-500/10 hover:text-blue-400"
                  >
                    <Mic className="h-4 w-4" />
                  </Button>
                </div>

                {selectedMode !== "basic" && selectedFiles.length > 0 && (
                  <div className="mt-3 flex flex-wrap justify-center gap-2">
                    {selectedFiles.map((file, index) => (
                      <div
                        key={`${file.name}-${file.size}-${file.lastModified}-${index}`}
                        className="flex items-center gap-2 rounded-lg border border-blue-500/30 bg-blue-500/10 px-3 py-2 text-sm text-blue-200"
                      >
                        <span className="max-w-[220px] truncate" title={file.name}>
                          {file.name}
                        </span>

                        <button
                          type="button"
                          onClick={() => removeSelectedFile(index)}
                          className="rounded p-0.5 text-blue-300 transition-colors hover:bg-red-500/10 hover:text-red-400"
                          title={`Remove ${file.name}`}
                          aria-label={`Remove ${file.name}`}
                        >
                          <X className="h-4 w-4" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {renderManualModePanel()}

            {renderMessages()}
          </div>
        </div>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(e) => {
          const files = Array.from(e.target.files || [])
          setSelectedFiles(files)

          if (files.length > 0) {
            showToast(`${files.length} file${files.length > 1 ? "s" : ""} selected`)
          }
        }}
      />
    </div>
  )
}