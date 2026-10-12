/*
    GainFx — the tiny test effect for the ChordSmith VST host tests.
    GPL-3.0-or-later (see ../LICENSE). Not for production use.

    Halves the signal and reports a 64-sample latency (with a matching delay line), so the
    host's latency compensation can be verified: the output must stay aligned with the input.
*/

#include <juce_audio_processors/juce_audio_processors.h>

#include <algorithm>
#include <array>
#include <vector>

namespace {

class GainFxProcessor : public juce::AudioProcessor {
public:
    static constexpr int kLatency = 64;

    GainFxProcessor()
        : AudioProcessor(BusesProperties()
                             .withInput("Input", juce::AudioChannelSet::stereo(), true)
                             .withOutput("Output", juce::AudioChannelSet::stereo(), true)) {
        setLatencySamples(kLatency);
        for (auto& channel : ring) channel.assign(kLatency, 0.0f);
    }

    const juce::String getName() const override { return "GainFx"; }
    bool acceptsMidi() const override { return false; }
    bool producesMidi() const override { return false; }
    bool isMidiEffect() const override { return false; }
    double getTailLengthSeconds() const override { return 0.0; }
    juce::AudioProcessorEditor* createEditor() override { return nullptr; }
    bool hasEditor() const override { return false; }

    void prepareToPlay(double, int) override {
        for (auto& channel : ring) std::fill(channel.begin(), channel.end(), 0.0f);
        position = 0;
    }
    void releaseResources() override {}

    bool isBusesLayoutSupported(const BusesLayout& layouts) const override {
        return layouts.getMainInputChannelSet() == juce::AudioChannelSet::stereo() &&
               layouts.getMainOutputChannelSet() == juce::AudioChannelSet::stereo();
    }

    void processBlock(juce::AudioBuffer<float>& buffer, juce::MidiBuffer&) override {
        const int frames = buffer.getNumSamples();
        for (int frame = 0; frame < frames; ++frame) {
            for (int channel = 0; channel < buffer.getNumChannels(); ++channel) {
                auto& line = ring[static_cast<size_t>(juce::jmin(1, channel))];
                const float delayed = line[static_cast<size_t>(position)];
                line[static_cast<size_t>(position)] = buffer.getSample(channel, frame) * 0.5f;
                buffer.setSample(channel, frame, delayed);
            }
            position = (position + 1) % kLatency;
        }
    }

    int getNumPrograms() override { return 1; }
    int getCurrentProgram() override { return 0; }
    void setCurrentProgram(int) override {}
    const juce::String getProgramName(int) override { return "Default"; }
    void changeProgramName(int, const juce::String&) override {}

    void getStateInformation(juce::MemoryBlock&) override {}
    void setStateInformation(const void*, int) override {}

private:
    std::array<std::vector<float>, 2> ring;
    int position = 0;
};

}  // namespace

juce::AudioProcessor* JUCE_CALLTYPE createPluginFilter() {
    return new GainFxProcessor();
}
