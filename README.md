# ScribeSense

**An OS-Level Conversational Accessibility Layer for Dyslexic Users**

ScribeSense is an intelligent, adaptive interaction layer that sits above the operating system and works consistently across all applications — word processors, spreadsheets, presentation software, web browsers, the file system, and media viewers — rather than being limited to a single app.

It passively observes typing and interaction patterns to identify signs consistent with dyslexia, infers the specific sub-type of difficulty a user is exhibiting (letter-reversal-dominant, phonetic-spelling-dominant, or visual-crowding-sensitive), and proactively — but conversationally — offers to help. Interface changes such as font, spacing, or colour adjustments are applied only after the user confirms, never silently.

> "It looks like you're having trouble with this — would you like me to adjust the font and colours?"

## Why

Existing OS accessibility tools (screen readers, magnifiers) are built almost entirely for vision and hearing impairments. The few settings relevant to dyslexia have to be manually discovered, navigated to, and configured — itself a difficult, text-heavy task for someone struggling with exactly that kind of interface. ScribeSense removes that burden by detecting difficulty as it happens and meeting the user where they are.

## Core Features

- **Passive pattern detection** — recognizes recurring letter-order mistakes, phonetic misspellings, and repeated corrections without requiring manual setup or diagnosis.
- **Sub-type inference** — classifies the dominant difficulty (reversal / phonetic / visual-crowding) to tailor assistance.
- **Conversational, consent-based adaptation** — asks before changing anything; never silently modifies the interface.
- **Phonetic search correction** — intercepts search queries system-wide and returns results for the intended word, not the literal typed spelling.
- **Voice-activated assistance mode** — a dedicated hotkey triggers natural-language command execution (e.g. "move this file from folder A to folder B"), bypassing reading/typing-heavy interaction.
- **Personalized, evolving profile** — assistance becomes more targeted over time as the system learns a user's specific tendencies.

## System Architecture

```
Interaction Monitoring Layer → Pattern & Sub-type Classifier → Conversational Check-in Agent
                                          │                              │
                                          ▼                              ▼
                            Personalised User Profile Store    Adaptation Engine (Font/Spacing/Colour)
                                                                          │
                              Voice Command Mode (ASR+NLU) → Phonetic Search Correction Module
```

## Planned Tech Stack

| Layer | Technology |
|---|---|
| OS-level hook service | Python / C++ |
| Pattern & sub-type classification | scikit-learn / lightweight PyTorch model |
| Phonetic query correction | Soundex / Metaphone + edit-distance matching |
| Voice command mode | Whisper / OS-native speech-to-text APIs |
| Personalized profile storage | SQLite (local) |

## Project Status

This project is in **Phase-I (Proposal & Design)** as part of a PBL (Project-Based Learning) course integrating **Operating Systems** and **DBMS** concepts.

- [x] Literature review on dyslexia sub-types
- [x] Requirements and system architecture defined
- [ ] Interaction-monitoring module implementation
- [ ] Sub-type classification model
- [ ] Conversational check-in UI + adaptation engine
- [ ] Phonetic search-query correction module
- [ ] Voice-activated command mode
- [ ] Cross-application integration testing

## Team

| Name | Role |
|---|---|
| Alisha Nadeem | Team Lead — Architecture, coordination |
| Sukhmani Kaur Ahluwalia | Research — Dyslexia sub-type study |
| Rayan Rahat Afzal | Backend/DB — Profile store design |
| Shivaansh Gusain | Systems — OS-hook & monitoring design |

**Mentor:** (Prof.) Dr. Vikas Tripathi

**Team ID:** OSDBMS-V-2026-T003 · Department of Computer Science & Engineering, Graphic Era (Deemed to be University), Dehradun

## References

- International Dyslexia Association — [Definition of Dyslexia](https://dyslexiaida.org)
- British Dyslexia Association — [Dyslexia Style Guide](https://www.bdadyslexia.org.uk)
- Rello, L. and Baeza-Yates, R., "Good Fonts for Dyslexia," *ACM ASSETS*, 2013.
- Microsoft Learn — [Windows Accessibility Settings Documentation](https://learn.microsoft.com)
