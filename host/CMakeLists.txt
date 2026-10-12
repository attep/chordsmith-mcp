cmake_minimum_required(VERSION 3.22)
project(chordsmith-vst-host VERSION 0.1.0 LANGUAGES CXX)

# The ChordSmith VST host is GPL-3.0-or-later (see LICENSE): it is a separate process and is
# never linked into the MIT-licensed ChordSmith server. JUCE is used under its GPLv3 option.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

option(BUILD_TEST_PLUGINS "Build the tiny test plugins (ToneSynth, GainFx) used by the tests" OFF)

include(FetchContent)
FetchContent_Declare(
  JUCE
  GIT_REPOSITORY https://github.com/juce-framework/JUCE.git
  GIT_TAG 7.0.12
  GIT_SHALLOW TRUE
)
FetchContent_MakeAvailable(JUCE)

juce_add_console_app(vst_host PRODUCT_NAME "vst_host")
target_sources(vst_host PRIVATE src/main.cpp)
target_compile_definitions(vst_host PRIVATE
  JUCE_WEB_BROWSER=0
  JUCE_USE_CURL=0
  JUCE_PLUGINHOST_VST3=1
  JUCE_PLUGINHOST_VST=0
  JUCE_PLUGINHOST_AU=0
  JUCE_PLUGINHOST_LADSPA=0
  JUCE_ALSA=0
  JUCE_JACK=0
  JUCE_MODAL_LOOPS_PERMITTED=1
  JUCE_USE_FLAC=0
  JUCE_USE_OGGVORBIS=0
  JUCE_USE_MP3AUDIOFORMAT=0
  JUCE_DISPLAY_SPLASH_SCREEN=0
  JUCE_STRICT_REFCOUNTEDPOINTER=1
)
target_link_libraries(vst_host PRIVATE
  juce::juce_audio_processors
  juce::juce_audio_formats
  juce::juce_audio_basics
  juce::juce_core
  juce::juce_data_structures
  juce::juce_events
  juce::juce_graphics
  juce::juce_gui_basics
  PUBLIC
  juce::juce_recommended_config_flags
  juce::juce_recommended_lto_flags
  juce::juce_recommended_warning_flags
)

if(BUILD_TEST_PLUGINS)
  juce_add_plugin(ToneSynth
    COMPANY_NAME "ChordSmith"
    PLUGIN_MANUFACTURER_CODE Csmt
    PLUGIN_NAME "ToneSynth"
    PLUGIN_CODE Tsyn
    FORMATS VST3
    IS_SYNTH TRUE
    NEEDS_MIDI_INPUT TRUE
    NEEDS_MIDI_OUTPUT FALSE
    IS_MIDI_EFFECT FALSE
    COPY_PLUGIN_AFTER_BUILD FALSE
    VST3_CATEGORIES Instrument Synth)
  target_sources(ToneSynth PRIVATE testplugins/ToneSynth.cpp)
  target_compile_definitions(ToneSynth PRIVATE
    JUCE_WEB_BROWSER=0 JUCE_USE_CURL=0 JUCE_DISPLAY_SPLASH_SCREEN=0 JUCE_ALSA=0 JUCE_JACK=0
    JUCE_VST3_CAN_REPLACE_VST2=0)
  target_link_libraries(ToneSynth PRIVATE
    juce::juce_audio_processors
    juce::juce_audio_basics
    juce::juce_gui_basics
    PUBLIC juce::juce_recommended_config_flags)

  juce_add_plugin(GainFx
    COMPANY_NAME "ChordSmith"
    PLUGIN_MANUFACTURER_CODE Csmt
    PLUGIN_NAME "GainFx"
    PLUGIN_CODE Gnfx
    FORMATS VST3
    IS_SYNTH FALSE
    NEEDS_MIDI_INPUT FALSE
    NEEDS_MIDI_OUTPUT FALSE
    IS_MIDI_EFFECT FALSE
    COPY_PLUGIN_AFTER_BUILD FALSE
    VST3_CATEGORIES Fx)
  target_sources(GainFx PRIVATE testplugins/GainFx.cpp)
  target_compile_definitions(GainFx PRIVATE
    JUCE_WEB_BROWSER=0 JUCE_USE_CURL=0 JUCE_DISPLAY_SPLASH_SCREEN=0 JUCE_ALSA=0 JUCE_JACK=0
    JUCE_VST3_CAN_REPLACE_VST2=0)
  target_link_libraries(GainFx PRIVATE
    juce::juce_audio_processors
    juce::juce_audio_basics
    juce::juce_gui_basics
    PUBLIC juce::juce_recommended_config_flags)
endif()
