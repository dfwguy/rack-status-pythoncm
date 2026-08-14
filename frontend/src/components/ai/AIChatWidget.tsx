/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 */
import { useState, useCallback } from "react";
import Box from "@mui/material/Box";
import Fab from "@mui/material/Fab";
import Tooltip from "@mui/material/Tooltip";
import IconButton from "@mui/material/IconButton";
import Badge from "@mui/material/Badge";
import { Text } from "@nvidia/foundations-react-core";
import { Bot, Minus, Maximize2, Minimize2, X } from "lucide-react";
import { useAIChat } from "../../context/AIChatContext";
import ChatWindow from "./ChatWindow";

const DEFAULT_WIDTH = 400;
const DEFAULT_HEIGHT = 520;
const MIN_WIDTH = 320;
const MIN_HEIGHT = 360;

export default function AIChatWidget() {
  const { isOpen, isMinimized, setOpen, setMinimized, messages } = useAIChat();
  const [isExpanded, setIsExpanded] = useState(false);
  const [size, setSize] = useState({ width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT });
  const [isDragging, setIsDragging] = useState(false);
  const [position, setPosition] = useState({ x: -1, y: -1 });
  const [dragStart, setDragStart] = useState({ x: 0, y: 0, posX: 0, posY: 0 });

  const handleOpen = useCallback(() => {
    if (position.x < 0) {
      setPosition({
        x: window.innerWidth - DEFAULT_WIDTH - 24,
        y: window.innerHeight - DEFAULT_HEIGHT - 80,
      });
    }
    setOpen(true);
    setMinimized(false);
  }, [position.x, setOpen, setMinimized]);

  const handleMinimize = useCallback(() => {
    setMinimized(true);
  }, [setMinimized]);

  const handleClose = useCallback(() => {
    setOpen(false);
    setMinimized(false);
    setIsExpanded(false);
  }, [setOpen, setMinimized]);

  const handleToggleExpand = useCallback(() => {
    setIsExpanded((prev) => !prev);
  }, []);

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      setIsDragging(true);
      setDragStart({ x: e.clientX, y: e.clientY, posX: position.x, posY: position.y });
    },
    [position],
  );

  const handleMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (!isDragging) return;
      const dx = e.clientX - dragStart.x;
      const dy = e.clientY - dragStart.y;
      setPosition({ x: dragStart.posX + dx, y: dragStart.posY + dy });
    },
    [isDragging, dragStart],
  );

  const handleMouseUp = useCallback(() => {
    setIsDragging(false);
  }, []);

  const handleResize = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      const startX = e.clientX;
      const startY = e.clientY;
      const startW = size.width;
      const startH = size.height;

      const onMove = (ev: MouseEvent) => {
        const newW = Math.max(MIN_WIDTH, startW + (ev.clientX - startX));
        const newH = Math.max(MIN_HEIGHT, startH + (ev.clientY - startY));
        setSize({ width: newW, height: newH });
      };
      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [size],
  );

  const hasMessages = messages.length > 0;

  if (!isOpen || isMinimized) {
    return (
      <Tooltip title="AI Assistant" placement="left">
        <Fab
          aria-label="AI Assistant"
          onClick={handleOpen}
          sx={{
            position: "fixed",
            bottom: 24,
            right: 24,
            zIndex: 1400,
            bgcolor: "#76B900",
            color: "#fff",
            "&:hover": { bgcolor: "#5a9000" },
          }}
        >
          <Badge variant="dot" color="error" invisible={!hasMessages || isOpen}>
            <Bot size={24} />
          </Badge>
        </Fab>
      </Tooltip>
    );
  }

  if (isExpanded) {
    return (
      <Box
        role="dialog"
        aria-label="AI assistant"
        sx={{
          position: "fixed",
          inset: 0,
          zIndex: 1500,
          display: "flex",
          flexDirection: "column",
          bgcolor: "background.paper",
        }}
      >
        <WindowHeader
          onMinimize={handleMinimize}
          onClose={handleClose}
          onToggleExpand={handleToggleExpand}
          isExpanded
        />
        <ChatWindow />
      </Box>
    );
  }

  return (
    <Box
      role="dialog"
      aria-label="AI assistant"
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      onMouseLeave={handleMouseUp}
      sx={{
        position: "fixed",
        left: position.x,
        top: position.y,
        width: size.width,
        height: size.height,
        zIndex: 1400,
        display: "flex",
        flexDirection: "column",
        bgcolor: "background.paper",
        borderRadius: 2,
        boxShadow: 8,
        border: "1px solid",
        borderColor: "divider",
        overflow: "hidden",
        userSelect: isDragging ? "none" : "auto",
      }}
    >
      <WindowHeader
        onMinimize={handleMinimize}
        onClose={handleClose}
        onToggleExpand={handleToggleExpand}
        isExpanded={false}
        onMouseDown={handleMouseDown}
      />
      <ChatWindow />
      <Box
        onMouseDown={handleResize}
        sx={{
          position: "absolute",
          bottom: 0,
          right: 0,
          width: 16,
          height: 16,
          cursor: "nwse-resize",
        }}
      />
    </Box>
  );
}

function WindowHeader({
  onMinimize,
  onClose,
  onToggleExpand,
  isExpanded,
  onMouseDown,
}: {
  onMinimize: () => void;
  onClose: () => void;
  onToggleExpand: () => void;
  isExpanded: boolean;
  onMouseDown?: (e: React.MouseEvent) => void;
}) {
  return (
    <Box
      onMouseDown={onMouseDown}
      sx={{
        display: "flex",
        alignItems: "center",
        gap: 1,
        px: 1.5,
        py: 1,
        bgcolor: "#76B900",
        color: "#fff",
        cursor: isExpanded ? "default" : "grab",
        userSelect: "none",
        flexShrink: 0,
        "&:active": { cursor: isExpanded ? "default" : "grabbing" },
      }}
    >
      <Bot size={18} />
      <Text kind="body/bold/sm" style={{ flex: 1, color: "#fff" }}>
        AI Assistant
      </Text>

      <Tooltip title="Minimize">
        <IconButton size="small" onClick={onMinimize} sx={{ color: "inherit" }}>
          <Minus size={16} />
        </IconButton>
      </Tooltip>
      <Tooltip title={isExpanded ? "Restore" : "Expand"}>
        <IconButton size="small" onClick={onToggleExpand} sx={{ color: "inherit" }}>
          {isExpanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
        </IconButton>
      </Tooltip>
      <Tooltip title="Close">
        <IconButton size="small" onClick={onClose} sx={{ color: "inherit" }}>
          <X size={16} />
        </IconButton>
      </Tooltip>
    </Box>
  );
}
