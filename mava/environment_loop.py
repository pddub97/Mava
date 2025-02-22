# python3
# Copyright 2021 InstaDeep Ltd. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A simple multi-agent-system-environment training loop."""
import copy
import logging
import time
from typing import Any, Dict, Tuple

import acme
import dm_env
import jax
import jax.numpy as jnp
import numpy as np
from acme.utils import counting, loggers

import mava
from mava.utils.checkpointing_utils import update_best_checkpoint, update_evaluator_net
from mava.utils.training_utils import check_count_condition
from mava.utils.wrapper_utils import generate_zeros_from_spec


class ParallelEnvironmentLoop(acme.core.Worker):
    """A parallel MARL environment loop.

    This takes `Environment` and `Executor` instances and coordinates their
    interaction. Executors are updated if `should_update=True`. This can be used as:
        loop = EnvironmentLoop(environment, executor)
        loop.run(num_episodes)
    A `Counter` instance can optionally be given in order to maintain counts
    between different Mava components. If not given a local Counter will be
    created to maintain counts between calls to the `run` method.
    A `Logger` instance can also be passed in order to control the output of the
    loop. If not given a platform-specific default logger will be used as defined
    by utils.loggers.make_default_logger from acme. A string `label` can be passed
    to easily change the label associated with the default logger; this is ignored
    if a `Logger` instance is given.
    """

    def __init__(
        self,
        environment: dm_env.Environment,
        executor: mava.core.Executor,
        counter: counting.Counter = None,
        logger: loggers.Logger = None,
        should_update: bool = True,
        label: str = "parallel_environment_loop",
    ):
        """Parallel environment loop init

        Args:
            environment: an environment
            executor: a Mava executor
            counter: an optional counter. Defaults to None.
            logger: an optional counter. Defaults to None.
            should_update: should update. Defaults to True.
            label: optional label. Defaults to "parallel_environment_loop".
        """
        # Internalize agent and environment.
        self._environment = environment
        self._executor = executor
        self._counter = counter or counting.Counter()
        self._logger = logger or loggers.make_default_logger(label)

        self._should_update = should_update
        self._running_statistics: Dict[str, float] = {}

        # We need this to schedule evaluation/test runs
        self._last_evaluator_run_t = -1

    def _get_actions(self, timestep: dm_env.TimeStep) -> Any:
        return self._executor.select_actions(timestep.observation)

    def _get_running_stats(self) -> Dict:
        return self._running_statistics

    def _compute_step_statistics(self, rewards: Dict[str, float]) -> None:
        pass

    def _compute_episode_statistics(
        self,
        episode_returns: Dict[str, float],
        episode_steps: int,
        start_time: float,
    ) -> None:
        pass

    def record_counts(self, episode_steps: int) -> counting.Counter:
        """Record latest counts"""
        # Record counts.
        counts = self._counter.increment(episodes=1, steps=episode_steps)
        return counts

    def run_episode(self) -> loggers.LoggingData:
        """Run one episode.

        Each episode is a loop which interacts first with the environment to get a
        dictionary of observations and then give those observations to the executor
        in order to retrieve an action for each agent in the system.

        Returns:
            An instance of `loggers.LoggingData`.
        """

        # Reset any counts and start the environment.
        start_time = time.time()
        episode_steps = 0

        timestep = self._environment.reset()

        if type(timestep) == tuple:
            timestep, env_extras = timestep
        else:
            env_extras = {}

        # Make the first observation.
        self._executor.observe_first(timestep, extras=env_extras)
        print('start running episode')
        # For evaluation, this keeps track of the total undiscounted reward
        # for each agent accumulated during the episode.
        rewards: Dict[str, float] = {}
        rewards_SOC: Dict[str, float] = {}
        rewards_loading: Dict[str, float] = {}
        episode_returns: Dict[str, float] = {}
        episode_returns_SOC: Dict[str, float] = {}
        episode_returns_loading: Dict[str, float] = {}
        #actions: Dict[str, float] = {}
        for agent, spec in self._environment.reward_spec().items():
            rewards.update({agent: generate_zeros_from_spec(spec)})
            rewards_SOC.update({agent: generate_zeros_from_spec(spec)})
            rewards_load.update({agent: generate_zeros_from_spec(spec)})
            episode_returns.update({agent: generate_zeros_from_spec(spec)})
            episode_returns_SOC.update({agent: generate_zeros_from_spec(spec)})
            episode_returns_loading.update({agent: generate_zeros_from_spec(spec)})
            #actions.update({agent: generate_zeros_from_spec(spec)})
        print('dict created')
        # Run an episode.
        while not timestep.last():

            # Generate an action from the agent's policy and step the environment.
            actions = self._get_actions(timestep)

            if type(actions) == tuple:
                # Return other action information
                # e.g. the policy information.
                env_actions, _ = actions
            else:
                env_actions = actions

            timestep = self._environment.step(env_actions)

            if type(timestep) == tuple:
                timestep, env_extras = timestep
            else:
                env_extras = {}
            print('extract reward')
            rewards = timestep.reward
            print('extract reward SOC')
            rewards_SOC = self._environment.world.SOC_reward
            rewards_loading = self._environment.world.loading_reward

            # Have the agent observe the timestep and let the actor update itself.
            self._executor.observe(
                actions, next_timestep=timestep, next_extras=env_extras
            )

            if self._should_update:
                self._executor.update()

            # Book-keeping.
            episode_steps += 1
            print('step statistics')
            self._compute_step_statistics(rewards)

            for agent, reward in rewards.items():
                episode_returns[agent] = episode_returns[agent] + reward
                print('assigning rewards to agents')
                episode_returns_SOC[agent] = episode_returns_SOC[agent] + rewards_SOC[agent]
                episode_returns_loading[agent] = episode_returns_loading[agent] + rewards_loading[agent]
        print('ep statistics')
        self._compute_episode_statistics(
            episode_returns,
            episode_returns_SOC,
            episode_returns_loading
            episode_steps,
            start_time,
        )
        print('ep statistics end')
        if self._get_running_stats():
            return self._get_running_stats()
        else:
            counts = self.record_counts(episode_steps)

            # Collect the results and combine with counts.
            steps_per_second = episode_steps / (time.time() - start_time)
            print('collecting results')
            result = {
                "episode_length": episode_steps,
                "mean_episode_return": np.mean(list(episode_returns.values())),
                "mean_episode_return_SOC": np.mean(list(episode_returns_SOC.values())),
                "mean_episode_return_loading": np.mean(list(episode_returns_loading.values())),
                "steps_per_second": steps_per_second,
            }
            print('upd counts')
            result.update(counts)
            print('end')
            return result

    def run_episode_and_log(self) -> loggers.LoggingData:
        """Run an episode and log the results"""

        results = self.run_episode()
        print('bef logs')
        self._logger.write(results)
        print('after logs logs')
        return results

    def run(self) -> None:  # pragma: no cover # noqa: C901
        """Run the environment loop."""

        def should_run_loop(eval_interval_condition: Tuple) -> bool:
            """Check if the eval loop should run in current step.

            Args:
                eval_interval_condition : tuple containing interval key and count.

            Returns:
                a bool indicating if eval should run.
            """
            should_run_loop = False
            eval_interval_key, eval_interval_count = eval_interval_condition
            counts = self._executor.store.executor_counts

            if counts:
                count = counts[eval_interval_key]
                # We run eval loops around every eval_interval_count (not exactly
                # every eval_interval_count due to latency in getting updated counts).
                should_run_loop = (
                    (count - self._last_evaluator_run_t) / eval_interval_count
                ) >= 1.0
                if should_run_loop:
                    self._last_evaluator_run_t = int(count)
                    print(
                        "Running eval loop at executor step: "
                        + f"{self._last_evaluator_run_t}"
                    )

            return should_run_loop

        environment_loop_schedule = self._executor._evaluator and (
            self._executor.store.evaluation_interval is not None
        )

        if environment_loop_schedule:
            eval_interval_condition = check_count_condition(
                self._executor.store.evaluation_interval
            )
            eval_duration_condition = check_count_condition(
                self._executor.store.evaluation_duration
            )
            evaluation_duration = eval_duration_condition[1]

        def run_evaluation(results: Any) -> None:  # pragma: no cover
            """Calculate the absolute metric"""
            logging.exception("Calculating the absolute metric")
            eval_result: Dict[str, Any] = {}  # create a dict with checkpointing_metric
            for metric in self._executor.store.global_config.checkpointing_metric:
                used_results = copy.deepcopy(results)
                # update the evaluator network
                update_evaluator_net(executor=self._executor, metric=metric)
                eval_returns = []
                for _ in range(
                    self._executor.store.global_config.absolute_metric_duration
                ):
                    # Add consecutive evaluation run data
                    result = self.run_episode()
                    if "return" in metric:
                        eval_returns.append(result[metric])
                    # Sum results for computing mean after all evaluation runs.
                    used_results = jax.tree_map(
                        lambda x, y: x + y, used_results, result
                    )
                # compute the mean over all evaluation runs
                used_results = jax.tree_map(
                    lambda x: x
                    / self._executor.store.global_config.absolute_metric_duration,
                    used_results,
                )
                if "return" in metric:
                    eval_result.update({f"eval_{metric}": jnp.array(eval_returns)})
                # Check for extra logs
                if hasattr(self._environment, "get_interval_stats"):
                    interval_stats = self._environment.get_interval_stats()
                    used_results.update(interval_stats)
                    if metric in interval_stats.keys():
                        interval_stats_json = {
                            "eval_" + str(k): v for k, v in interval_stats.items()
                        }
                        # Add interval stats to dictionary for json logging
                        eval_result.update(
                            jax.tree_util.tree_map(
                                lambda leaf: jnp.array([leaf]), interval_stats_json
                            )
                        )
                logging.exception(
                    f"Absolute metric for {metric} is equal {used_results[metric]}"
                )
                logging.exception(f"Additional results {used_results}")
            used_results.update(eval_result)
            self._logger.write(used_results)
            logging.exception("Terminate the system")
            self._executor.store.executor_parameter_client.set_and_wait(
                {"terminate": True}
            )

        def step_executor() -> None:  # pragma: no cover
            if (not environment_loop_schedule) or (
                should_run_loop(eval_interval_condition)
            ):
                if environment_loop_schedule:
                    # Get first result dictionary
                    results = self.run_episode()

                    # Check if requires calculating the absolute metric
                    global_config = self._executor.store.global_config
                    run_absolute_metric = (
                        hasattr(global_config, "checkpoint_best_perf")
                        and self._executor.store.global_config.absolute_metric
                        and (
                            results["executor_steps"]
                            >= global_config.termination_condition["executor_steps"]
                        )
                    )

                    if run_absolute_metric:
                        logging.exception(
                            f"Executor has reached {results['executor_steps']} steps"
                        )
                        run_evaluation(results=results)

                    # Initialise list for capturing episode returns
                    eval_returns = []

                    eval_returns.append(results["raw_episode_return"])
                    for _ in range(evaluation_duration - 1):
                        # Add consecutive evaluation run data
                        result = self.run_episode()
                        eval_returns.append(result["raw_episode_return"])
                        # Sum results for computing mean after all evaluation runs.
                        results = jax.tree_map(lambda x, y: x + y, results, result)
                    # compute the mean over all evaluation runs
                    results = jax.tree_map(lambda x: x / evaluation_duration, results)

                    # Log evaluation interval results for json logging
                    # all results with the `eval` appended will be logged
                    # by the json logger.
                    eval_result = {
                        "eval_step_count": jnp.array(self._last_evaluator_run_t),
                        "eval_return": jnp.array(eval_returns),
                    }

                    # Check for extra logs
                    if hasattr(self._environment, "get_interval_stats"):
                        interval_stats = self._environment.get_interval_stats()
                        results.update(interval_stats)
                        interval_stats_json = {
                            "eval_" + str(k): v for k, v in interval_stats.items()
                        }

                        # Add interval stats to dictionary for json logging
                        eval_result.update(
                            jax.tree_util.tree_map(
                                lambda leaf: jnp.array([leaf]), interval_stats_json
                            )
                        )

                    results.update(eval_result)
                    self._logger.write(results)

                    # ideally this would be executor.has(BestCheckpointer),
                    # but that causes circular import
                    if hasattr(global_config, "checkpoint_best_perf") and (
                        global_config.checkpoint_best_perf
                        or global_config.absolute_metric
                    ):
                        # Best_performance_update
                        for (
                            metric,
                            best_performance,
                        ) in self._executor.store.checkpointing_metric.items():
                            assert (
                                metric in results.keys()
                            ), f"The metric, {metric}, chosen for checkpointing doesn't exist.\
                                 This experiment has only the following metrics:\
                                 {results.keys()}"

                            if (
                                best_performance is None
                                or best_performance < results[metric]  # type: ignore
                            ):
                                self._executor.store.checkpointing_metric[
                                    metric
                                ] = update_best_checkpoint(
                                    self._executor, results, metric
                                )
                else:
                    result = self.run_episode()
                    # Log the given results.
                    self._logger.write(result)
            else:
                # Note: We assume that the evaluator will be running less
                # than once per second.
                time.sleep(1)
            # We need to get the latest counts if we are using eval intervals.
            if environment_loop_schedule:
                self._executor.force_update()

        while True:
            try:
                step_executor()

            except Exception as e:
                if self._executor._evaluator:
                    logging.exception(
                        f"{e}: Experiment terminated due to an error on the evaluator."
                    )
                    self._executor.store.executor_parameter_client.set_and_wait(
                        {"terminate": True}
                    )
                else:
                    logging.exception(f"{e}: an executor failed.")
                    self._executor.store.executor_parameter_client.add_and_wait(
                        {"num_executor_failed": 1}
                    )
                self._executor.force_update()
                break
