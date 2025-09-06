# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an AstrBot plugin for a "Sea Turtle Soup" (海龟汤) reasoning game. It's a Chinese puzzle game where players ask yes/no questions to deduce a story.

## Architecture

- **Single-file plugin**: All functionality in `main.py` (1861 lines)
- **AstrBot framework**: Built on AstrBot >= v3.4.36
- **Thread-safe storage**: `ThreadSafeStoryStorage` class for managing puzzle usage
- **Game state management**: `GameState` class tracks active games per group
- **LLM integration**: Uses AstrBot's Provider interface for AI functionality
- **Session management**: Uses `session_waiter` for conversation control

## Key Components

- **Network puzzle database**: `network_soupai.json` with ~300 pre-scraped puzzles
- **Configuration**: `_conf_schema.json` defines plugin settings
- **Metadata**: `metadata.yaml` for plugin registration

## Development Commands

This is an AstrBot plugin, so development involves:
1. **Testing**: No formal test framework found - manual testing required
2. **Linting**: No specific linting configuration found
3. **Building**: No build process - it's a Python plugin file
4. **Installation**: Copy to `AstrBot/data/plugins/` directory

## Plugin Structure

- **Main class**: Registered with `@register` decorator
- **Command handlers**: Decorated with `@filter.command`
- **Session handlers**: Use `@session_waiter` for conversation flow
- **LLM integration**: Uses `self.context.get_using_provider()` and `self.context.get_provider_by_id()`

## Important Patterns

- **Thread safety**: Uses `threading.Lock` for shared state
- **Persistence**: JSON files for usage tracking
- **Error handling**: Comprehensive try-catch blocks with logging
- **Configuration**: Managed through AstrBot's plugin configuration system

## Development Workflow

1. Modify `main.py`
2. Reload plugin in AstrBot WebUI
3. Test commands in chat
4. Check logs for errors

## Key Files

- `main.py:1-1861` - Core plugin implementation
- `network_soupai.json` - Puzzle database
- `_conf_schema.json:1-49` - Configuration schema
- `metadata.yaml:1-11` - Plugin metadata