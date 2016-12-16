# Copyright 2016 Chr. Hansen A/S and The Novo Nordisk Foundation Center for Biosustainability, DTU.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

# http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from IProgress import ProgressBar, Bar, Percentage
from bokeh.layouts import column
from bokeh.models import FactorRange, Range1d, LinearAxis
from bokeh.plotting import figure, show
from bokeh.charts import Line

from pandas import DataFrame

from cobra.core import Metabolite
from cameo.core.solver_based_model import SolverBasedModel as Model
from cameo.core.result import Result
from cameo.util import TimeMachine
from cameo.flux_analysis.analysis import flux_variability_analysis, FluxVariabilityResult
from cameo.flux_analysis.simulation import pfba, fba
from cameo.exceptions import SolveError

from marsi.processing.models import search_metabolites, apply_antimetabolite
from marsi.utils import frange

BASE_ELEMENTS = ["C", "N"]


class MetaboliteKnockoutFitness(Result):
    def __init__(self, fitness_data_frame, *args, **kwargs):
        super(MetaboliteKnockoutFitness, self).__init__(*args, **kwargs)
        assert isinstance(fitness_data_frame, DataFrame)
        self._data_frame = fitness_data_frame

    @property
    def data_frame(self):
        return DataFrame(self._data_frame)

    def plot(self, grid=None, width=None, height=None, title=None, *args, **kwargs):
        data = self._data_frame.sort_values('fitness')
        data['x'] = data.index
        show(Line(data, 'x', 'fitness', title=title, plot_width=width, plot_height=height))

    def _repr_html_(self):
        return self.plot(height=500, width=12*len(self._data_frame), title="Fitness for metabolite Knockout")


def metabolite_knockout_fitness(model, simulation_method=pfba, compartments=None, elements=BASE_ELEMENTS,
                                objective=None, ndecimals=6, progress=False, ncarbons=2, steady_state=True,
                                **simulation_kwargs):
    assert isinstance(model, Model)
    fitness = DataFrame(columns=["fitness"]+elements)
    if compartments is None:
        compartments = list(model.compartments.keys())

    if progress:
        iterator = ProgressBar(maxval=len(model.metabolites), widgets=[Bar(), Percentage()])
    else:
        iterator = iter
    for met in iterator(model.metabolites):
        if met.compartment in compartments and met.elements.get("C", 0) > ncarbons:
            with TimeMachine() as tm:
                met.knock_out(tm, force_steady_state=steady_state)
                try:
                    solution = simulation_method(model, objective=objective, **simulation_kwargs)
                    fitness.loc[met.id] = [round(solution[objective], ndecimals)] + \
                                          [met.elements.get(el, 0) for el in elements]
                except SolveError:
                    fitness.loc[met.id] = [.0] + [met.elements.get(el, 0) for el in elements]

    return MetaboliteKnockoutFitness(fitness)


class MetaboliteKnockoutPhenotypeResult(MetaboliteKnockoutFitness):
    def __init__(self, phenotype_data_frame, *args, **kwargs):
        super(MetaboliteKnockoutPhenotypeResult, self).__init__(phenotype_data_frame, *args, **kwargs)

    def __getitem__(self, item):
        phenotype = {}
        if isinstance(item, (int, slice)):
            fva = self._data_frame['fva'].iloc[item]
        elif isinstance(item, str):
            fva = self._data_frame['fva'].loc[item]
        elif isinstance(item, Metabolite):
            fva = self._data_frame['fva'].loc[item.id]
        else:
            raise ValueError("%s is not a valid search retrieval")

        for reaction_id, row in fva.data_frame.iterrows():
            if row['upper_bound'] == 0 and row['lower_bound'] == 0:
                continue
            phenotype[reaction_id] = (row['upper_bound'], row['lower_bound'])

        return phenotype

    def _phenotype_plot(self, index, factors):
        plot = figure(title=index, y_range=FactorRange(factors))
        phenotype = self[index]

        y0 = [phenotype.get(f, [0, 0])[0] for f in factors]
        y1 = [phenotype.get(f, [0, 0])[1] for f in factors]

        x0 = [(i - .5) for i in range(len(factors))]
        x1 = [(i + .5) for i in range(len(factors))]

        plot.quad(x0, x1, y0, y1)

        return plot

    def plot(self, indexes=None, grid=None, width=None, height=None, title=None, conditions=None, *args, **kwargs):
        data = self.data_frame
        if conditions:
            data = data.query(conditions)
        if indexes:
            data = data.loc[indexes]

        factors = list(set(sum(data.phenotype.apply(lambda p: list(p.keys()))), []))
        plots = []

        for index in data.index:
            plots.append(self._phenotype_plot(index, factors))
        show(column(plots))


def metabolite_knockout_phenotype(model, compartments=None, objective=None, ndecimals=6, elements=BASE_ELEMENTS,
                                  progress=False, ncarbons=2, steady_state=True):
    assert isinstance(model, Model)
    phenotype = DataFrame(columns=['fitness', 'fva'] + elements)
    exchanges = model.exchanges

    if progress:
        iterator = ProgressBar(maxval=len(model.metabolites), widgets=[Bar(), Percentage()])
    else:
        iterator = iter

    for met in iterator(model.metabolites):
        if met.compartment in compartments and met.elements.get("C", 0) > ncarbons:
            with TimeMachine() as tm:
                met.knock_out(tm, force_steady_state=steady_state)
                fitness = fba(model, objective=objective)
                fva = flux_variability_analysis(model, reactions=exchanges, fraction_of_optimum=1)
                fva = FluxVariabilityResult(fva.data_frame.apply(round, args=(ndecimals,)))
                phenotype.loc[met.id] = [fitness, fva] + [met.elements.get(el, 0) for el in elements]

    return MetaboliteKnockoutPhenotypeResult(phenotype)


class SensitivityAnalysisResult(Result):
    def __init__(self, species_id, exchange_fluxes, steps, biomass_fluxes=None, biomass=None, *args, **kwargs):
        super(SensitivityAnalysisResult, self).__init__(*args, **kwargs)
        self._species_id = species_id
        self._exchange_fluxes = exchange_fluxes
        self._steps = steps
        self._biomass_fluxes = biomass_fluxes
        self._biomass = biomass

    @property
    def data_frame(self):
        if self._biomass is None:
            return DataFrame([self._steps, self._exchange_fluxes], index=["fraction", self._species_id]).T
        else:
            return DataFrame([self._steps, self._exchange_fluxes, self._biomass_fluxes],
                             index=["fraction", self._species_id, self._biomass.id]).T

    def plot(self, grid=None, width=None, height=None, *args, **kwargs):
        fig = figure(plot_width=305, plot_height=305, title=self._species_id,
                     x_axis_label='Inihibition level', y_axis_label="Accumulation Level",
                     toolbar_sticky=False)

        data = self.data_frame

        if self._biomass is None:
            fig.line(data['fraction'].apply(lambda v: 1 - v) * 100, data[self._species_id],
                     line_color='orange')

        else:

            fig.extra_y_ranges = {"growth_rate": Range1d(start=0, end=1)}
            fig.add_layout(LinearAxis(y_range_name="growth_rate", axis_label="Growth rate"), 'right')
            fig.line(data['fraction'].apply(lambda v: 1 - v) * 100, data[self._species_id]/data[self._biomass.id],
                     line_color='orange')
            fig.line(data['fraction'].apply(lambda v: 1 - v) * 100, data[self._biomass.id], line_color='green',
                     y_range_name="growth_rate")

        show(fig)


def sensitivity_analysis(model, metabolite, biomass=None, is_essential=False, steps=10, reference_dist=None,
                         simulation_method=fba, **simulation_kwargs):

    if reference_dist is None:
        simulation_kwargs['reference'] = simulation_kwargs.get('reference', None) or pfba(model, objective=biomass)

    species_id = metabolite.id[:-2]
    metabolites = search_metabolites(model, species_id)

    essential_metabolites = []
    if is_essential:
        essential_metabolites.append(metabolite)

    exchange_fluxes = []
    biomass_fluxes = []
    fractions = []

    for fraction in frange(0, 1.1, steps):
        with TimeMachine() as tm:
            exchanges = apply_antimetabolite(metabolites, essential_metabolites, simulation_kwargs['reference'],
                                             inhibition_fraction=fraction, competition_fraction=fraction,
                                             allow_accumulation=True, ignore_transport=True, time_machine=tm)

            flux_dist = simulation_method(model, **simulation_kwargs)

            if biomass is not None:
                biomass_fluxes.append(flux_dist[biomass])

            exchange_fluxes.append(sum(flux_dist[exchange] for exchange in exchanges))
            fractions.append(fraction)

    return SensitivityAnalysisResult(species_id, exchange_fluxes, fractions, biomass_fluxes, biomass)





